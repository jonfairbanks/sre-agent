"""Durable state: langgraph checkpoints, sessions, HITL audit, monitoring state.

Everything the bot needed to survive a restart used to live in process memory —
``MemorySaver``, ``InMemoryStore``, and ``api._sessions``. A pod replacement
orphaned every pending HITL approval: the Slack Approve button stayed live but
the session behind it was gone, so the click dead-ended and the proposed cluster
change could neither be applied nor properly rejected.

This module backs all of that with Postgres.

Degradation is deliberate. If ``DATABASE_URL`` is unset (local CLI use) or the
database is unreachable at startup, the process falls back to in-memory
equivalents and logs loudly rather than refusing to boot. The bot runs *inside*
the cluster hosting its own database, so a cluster problem must not also take
away the operator's ability to ask the bot about it. ``/health`` reports which
mode is live so the degradation is never silent.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from monitor_state import ReportDiff, StoredFinding, serialize_diff

log = logging.getLogger("sre-agent.persistence")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                text PRIMARY KEY,
    thread_id         text NOT NULL,
    status            text NOT NULL,
    source            text NOT NULL DEFAULT 'api',
    pending_decisions integer NOT NULL DEFAULT 1,
    pending_actions   jsonb,
    interrupt_data    jsonb,
    last_response     text,
    slack_message_ts  text,
    slack_channel     text,
    slack_thread_ts   text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Append-only. Nothing in this module issues UPDATE or DELETE against it: the
-- point is an immutable record of who authorised each cluster mutation.
CREATE TABLE IF NOT EXISTS hitl_audit (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    session_id text NOT NULL,
    thread_id  text,
    actor      text,
    actor_id   text,
    decision   text NOT NULL,
    source     text,
    tool_name  text,
    tool_args  jsonb,
    result     text
);
CREATE INDEX IF NOT EXISTS hitl_audit_session_idx ON hitl_audit (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS hitl_audit_ts_idx ON hitl_audit (ts DESC);

CREATE TABLE IF NOT EXISTS finding_state (
    fingerprint   text PRIMARY KEY,
    namespace     text NOT NULL DEFAULT '',
    kind          text NOT NULL DEFAULT '',
    resource_name text NOT NULL DEFAULT '',
    reason        text NOT NULL DEFAULT '',
    severity      text NOT NULL,
    title         text NOT NULL DEFAULT '',
    detail        text NOT NULL DEFAULT '',
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    times_seen    integer NOT NULL DEFAULT 1,
    resolved_at   timestamptz,
    ack_until     timestamptz
);
CREATE INDEX IF NOT EXISTS finding_state_open_idx
    ON finding_state (last_seen DESC) WHERE resolved_at IS NULL;

-- Maps a posted report to the fingerprints it covered, so the Slack "Ack"
-- button can carry a short opaque id instead of a fingerprint list that would
-- blow past Slack's 2000-character button value limit.
CREATE TABLE IF NOT EXISTS monitor_reports (
    report_id    text PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    fingerprints jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_meta (
    key   text PRIMARY KEY,
    value text
);
ALTER TABLE finding_state ADD COLUMN IF NOT EXISTS ignored_until timestamptz;
ALTER TABLE finding_state ADD COLUMN IF NOT EXISTS ignored_forever boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS monitor_checks (
    session_id text PRIMARY KEY,
    check_no bigint NOT NULL,
    observed_at timestamptz NOT NULL,
    analysis_valid boolean NOT NULL,
    coverage jsonb NOT NULL,
    report jsonb NOT NULL,
    diff jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS monitor_checks_time_idx ON monitor_checks (observed_at DESC);
CREATE TABLE IF NOT EXISTS finding_observations (
    session_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    check_no bigint NOT NULL,
    fingerprint text NOT NULL,
    status text NOT NULL,
    severity text NOT NULL,
    title text NOT NULL DEFAULT '',
    namespace text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT '',
    resource_name text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS finding_observations_history_idx
    ON finding_observations (fingerprint, observed_at DESC);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id text PRIMARY KEY,
    session_id text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL,
    next_attempt_at timestamptz NOT NULL,
    lease_until timestamptz,
    delivered_at timestamptz,
    message_ts text,
    last_error_type text
);
CREATE INDEX IF NOT EXISTS notification_outbox_due_idx
    ON notification_outbox (next_attempt_at) WHERE status <> 'delivered';
"""

# How far back a resolved finding is still remembered. Keeps flap detection
# working ("this came back for the 4th time this week") while bounding the table.
_RESOLVED_RETENTION_DAYS = 7


class NullDatabase:
    """No-op implementation used when Postgres is unavailable.

    Same surface as :class:`PostgresDatabase` so callers never branch on
    ``if db:``. Monitoring degrades to its old stateless behaviour (every run
    looks new) and the audit log is dropped — hence the startup warning.
    """

    kind = "memory"
    available = False

    def setup(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def save_session(self, session: dict) -> None:
        pass

    def load_session(self, session_id: str) -> Optional[dict]:
        return None

    def record_decision(self, **kwargs) -> None:
        log.info(
            "[NO AUDIT DB] HITL %s on session=%s by actor=%s tool=%s",
            kwargs.get("decision"), kwargs.get("session_id"),
            kwargs.get("actor"), kwargs.get("tool_name"),
        )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        return []

    def load_tracked_findings(self) -> dict[str, StoredFinding]:
        return {}

    def apply_diff(self, diff: ReportDiff, now: Optional[datetime] = None) -> None:
        pass

    def save_report(self, fingerprints: list[str]) -> Optional[str]:
        return None

    def ack_report(self, report_id: str, hours: int) -> int:
        return 0

    def next_check_number(self) -> int:
        return 0

    def record_monitor_check(self, session_id, check_no, report, data, diff, now, notification):
        return None

    def recent_monitor_checks(self, since=None, namespace='', limit=20):
        return []

    def finding_history(self, fingerprint, limit=20):
        return []

    def claim_pending_notifications(self, now, limit=10):
        return []

    def mark_notification_delivered(self, notification_id, message_ts, now, *, attempts=None):
        pass

    def reschedule_notification(self, notification_id, error_type, now, retry_after_seconds, *, attempts=None):
        pass

    def delivery_status(self):
        return {'available': False, 'pending': 0, 'in_flight': 0,
                'oldest_pending_at': None, 'last_delivered_at': None}

    def mute_report_finding(self, report_id, fingerprint_hash, hours, forever=False):
        return False

    def list_muted_findings(self, limit=50):
        return []


class PostgresDatabase:
    """Postgres-backed state. All SQL is parameterized; no value interpolation."""

    kind = "postgres"
    available = True

    def __init__(self, pool):
        self._pool = pool

    def setup(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(SCHEMA)
        log.info("Postgres schema ready (sessions, hitl_audit, finding_state, monitor_*)")

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:  # pragma: no cover - shutdown best effort
            log.debug("Connection pool close failed", exc_info=True)

    # -- sessions ---------------------------------------------------------

    def save_session(self, session: dict) -> None:
        """Write-through upsert of a session's durable fields."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, thread_id, status, source, pending_decisions,
                    pending_actions, interrupt_data, last_response,
                    slack_message_ts, slack_channel, slack_thread_ts, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    thread_id        = EXCLUDED.thread_id,
                    status           = EXCLUDED.status,
                    source           = EXCLUDED.source,
                    pending_decisions= EXCLUDED.pending_decisions,
                    pending_actions  = EXCLUDED.pending_actions,
                    interrupt_data   = EXCLUDED.interrupt_data,
                    last_response    = EXCLUDED.last_response,
                    slack_message_ts = EXCLUDED.slack_message_ts,
                    slack_channel    = EXCLUDED.slack_channel,
                    slack_thread_ts  = EXCLUDED.slack_thread_ts,
                    updated_at       = now()
                """,
                (
                    session["id"],
                    session["thread_id"],
                    session["status"],
                    session.get("source", "api"),
                    session.get("pending_decisions", 1),
                    Jsonb(session.get("pending_actions") or []),
                    Jsonb(session.get("interrupt_data") or []),
                    session.get("last_response", ""),
                    session.get("slack_message_ts"),
                    session.get("slack_channel"),
                    session.get("slack_thread_ts"),
                ),
            )

    def load_session(self, session_id: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT id, thread_id, status, source, pending_decisions,
                       pending_actions, interrupt_data, last_response,
                       slack_message_ts, slack_channel, slack_thread_ts
                  FROM sessions
                 WHERE id = %s
                """,
                (session_id,),
            )
            return cur.fetchone()

    # -- audit ------------------------------------------------------------

    def record_decision(
        self,
        session_id: str,
        decision: str,
        thread_id: str = "",
        actor: str = "",
        actor_id: str = "",
        source: str = "",
        tool_name: str = "",
        tool_args: Any = None,
        result: str = "",
    ) -> None:
        """Append one immutable HITL decision record."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO hitl_audit (
                    session_id, thread_id, actor, actor_id, decision,
                    source, tool_name, tool_args, result
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id, thread_id, actor, actor_id, decision,
                    source, tool_name, Jsonb(tool_args or {}), (result or "")[:4000],
                ),
            )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT ts, session_id, actor, actor_id, decision, source,
                       tool_name, tool_args, result
                  FROM hitl_audit
                 ORDER BY ts DESC
                 LIMIT %s
                """,
                (min(max(limit, 1), 500),),
            )
            return list(cur.fetchall())

    # -- monitoring finding state -----------------------------------------

    def load_tracked_findings(self) -> dict[str, StoredFinding]:
        """Open findings, plus recently-resolved and acked ones.

        Recently-resolved rows are included so a returning problem is reported
        as new again while keeping its cumulative ``times_seen``.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, severity, title, namespace, first_seen,
                       last_seen, times_seen, resolved_at, ack_until,
                       kind, resource_name, reason, detail, ignored_until, ignored_forever
                  FROM finding_state
                 WHERE resolved_at IS NULL
                    OR resolved_at > now() - make_interval(days => %s)
                    OR ack_until > now() OR ignored_until > now() OR ignored_forever
                """,
                (_RESOLVED_RETENTION_DAYS,),
            )
            rows = cur.fetchall()

        return {
            r["fingerprint"]: StoredFinding(
                fingerprint=r["fingerprint"],
                severity=r["severity"],
                title=r["title"],
                namespace=r["namespace"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                times_seen=r["times_seen"],
                resolved_at=r["resolved_at"],
                ack_until=r["ack_until"],
                kind=r['kind'], resource_name=r['resource_name'],
                reason=r['reason'], detail=r['detail'],
                ignored_until=r['ignored_until'], ignored_forever=r['ignored_forever'],
                mute_expired=(not r['ignored_forever'] and r['ignored_until'] is not None
                              and r['ignored_until'] <= datetime.now(timezone.utc)),
            )
            for r in rows
        }

    def apply_diff(self, diff: ReportDiff, now: Optional[datetime] = None) -> None:
        """Persist finding transitions without altering unverified incidents."""
        now = now or datetime.now(timezone.utc)
        with self._pool.connection() as conn, conn.transaction():
            self._apply_diff_on_connection(conn, diff, now)

    def _apply_diff_on_connection(self, conn, diff, now):
        seen = diff.active + diff.suppressed
        for delta in seen:
            f = delta.finding
            conn.execute(
                """
                INSERT INTO finding_state (
                    fingerprint, namespace, kind, resource_name, reason,
                    severity, title, detail, first_seen, last_seen,
                    times_seen, resolved_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (fingerprint) DO UPDATE SET
                    namespace     = EXCLUDED.namespace,
                    kind          = EXCLUDED.kind,
                    resource_name = EXCLUDED.resource_name,
                    reason        = EXCLUDED.reason,
                    severity      = EXCLUDED.severity,
                    title         = EXCLUDED.title,
                    detail        = EXCLUDED.detail,
                    last_seen     = EXCLUDED.last_seen,
                    times_seen    = EXCLUDED.times_seen,
                    resolved_at   = NULL,
                    ignored_until = CASE WHEN NOT finding_state.ignored_forever
                        AND finding_state.ignored_until <= EXCLUDED.last_seen
                        THEN NULL ELSE finding_state.ignored_until END,
                    -- A finding that had been resolved and came back
                    -- restarts its clock; one that never closed keeps
                    -- the earliest first_seen we know about.
                    first_seen    = CASE
                        WHEN finding_state.resolved_at IS NOT NULL
                            THEN EXCLUDED.first_seen
                        ELSE LEAST(finding_state.first_seen, EXCLUDED.first_seen)
                    END
                """,
                (
                    delta.fingerprint,
                    getattr(f, "namespace", "") or "",
                    getattr(f, "kind", "") or "",
                    getattr(f, "resource_name", "") or "",
                    getattr(f, "reason", "") or "",
                    f.severity,
                    (getattr(f, "title", "") or "")[:500],
                    (getattr(f, "detail", "") or "")[:4000],
                    delta.first_seen,
                    now,
                    delta.times_seen,
                ),
            )

        if diff.resolved:
            conn.execute(
                """
                UPDATE finding_state
                   SET resolved_at = %s,
                       ignored_until = CASE WHEN NOT ignored_forever AND ignored_until <= %s
                           THEN NULL ELSE ignored_until END
                 WHERE fingerprint = ANY(%s)
                """,
                (now, now, [r.fingerprint for r in diff.resolved]),
            )

    def save_report(self, fingerprints: list[str]) -> Optional[str]:
        """Record which fingerprints a posted report covered; return its id."""
        from psycopg.types.json import Jsonb

        report_id = uuid.uuid4().hex[:12]
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO monitor_reports (report_id, fingerprints) VALUES (%s, %s)",
                (report_id, Jsonb(list(fingerprints))),
            )
        return report_id

    def ack_report(self, report_id: str, hours: int) -> int:
        """Suppress every finding in a report for ``hours``. Returns rows acked."""
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        with self._pool.connection() as conn:
            cur = conn.execute(
                "SELECT fingerprints FROM monitor_reports WHERE report_id = %s",
                (report_id,),
            )
            row = cur.fetchone()
            if not row:
                return 0
            fingerprints = list(row["fingerprints"] or [])
            if not fingerprints:
                return 0
            cur = conn.execute(
                """
                UPDATE finding_state
                   SET ack_until = %s
                 WHERE fingerprint = ANY(%s)
                   AND resolved_at IS NULL
                """,
                (until, fingerprints),
            )
            return cur.rowcount

    def record_monitor_check(self, session_id, check_no, report, data, diff, now, notification):
        """Commit an observation, its state changes and its alert in one transaction.

        The session ID makes a retry idempotent. Raw collection data is deliberately
        omitted from history; it may contain logs or resource configuration.
        """
        from psycopg.types.json import Jsonb

        report_json = report.model_dump(mode='json')
        diff_json = serialize_diff(diff)
        notification_id = str(uuid.uuid4()) if notification else None
        with self._pool.connection() as conn, conn.transaction():
            inserted = conn.execute(
                '''INSERT INTO monitor_checks
                   (session_id, check_no, observed_at, analysis_valid, coverage, report, diff)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING session_id''',
                (session_id, check_no, now, report.analysis_valid, Jsonb(report_json.get('coverage', [])),
                 Jsonb(report_json), Jsonb(diff_json)),
            ).fetchone()
            if not inserted:
                row = conn.execute('SELECT id FROM notification_outbox WHERE session_id=%s',
                                   (session_id,)).fetchone()
                return row['id'] if row else None
            self._apply_diff_on_connection(conn, diff, now)
            for status in ('new', 'escalated', 'ongoing', 'suppressed', 'resolved', 'retained'):
                for entry in getattr(diff, status):
                    finding = getattr(entry, 'finding', entry)
                    conn.execute(
                        '''INSERT INTO finding_observations
                           (session_id,check_no,observed_at,fingerprint,status,severity,title,
                            namespace,kind,resource_name,reason,detail)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (session_id, check_no, now, entry.fingerprint, status, finding.severity,
                         finding.title[:500], getattr(finding, 'namespace', '') or '',
                         getattr(finding, 'kind', '') or '', getattr(finding, 'resource_name', '') or '',
                         getattr(finding, 'reason', '') or '', (getattr(finding, 'detail', '') or '')[:4000]),
                    )
            if notification:
                report_id = uuid.uuid4().hex[:12]
                covered = list(dict.fromkeys(d.fingerprint for d in diff.active + diff.suppressed + diff.retained))
                conn.execute('INSERT INTO monitor_reports(report_id,created_at,fingerprints) VALUES (%s,%s,%s)',
                             (report_id, now, Jsonb(covered)))
                payload = {**notification, 'report_id': report_id}
                conn.execute(
                    '''INSERT INTO notification_outbox
                       (id,session_id,payload,created_at,next_attempt_at) VALUES (%s,%s,%s,%s,%s)''',
                    (notification_id, session_id, Jsonb(payload), now, now),
                )
            cutoff = now - timedelta(days=30)
            conn.execute('DELETE FROM finding_observations WHERE observed_at < %s', (cutoff,))
            conn.execute('DELETE FROM monitor_checks WHERE observed_at < %s', (cutoff,))
            conn.execute("DELETE FROM notification_outbox WHERE status='delivered' AND delivered_at < %s", (cutoff,))
            # Keep report references for pending alerts, even after history retention.
            conn.execute('''DELETE FROM monitor_reports r WHERE created_at < %s
                            AND NOT EXISTS (SELECT 1 FROM notification_outbox o
                              WHERE o.status <> 'delivered' AND o.payload->>'report_id'=r.report_id)''', (cutoff,))
        return notification_id

    def recent_monitor_checks(self, since=None, namespace='', limit=20):
        since = since or datetime.now(timezone.utc) - timedelta(days=1)
        with self._pool.connection() as conn:
            return conn.execute(
                '''SELECT * FROM monitor_checks c WHERE observed_at >= %s
                   AND (%s='' OR EXISTS (SELECT 1 FROM finding_observations f
                       WHERE f.session_id=c.session_id AND f.namespace=%s))
                   ORDER BY observed_at DESC,session_id DESC LIMIT %s''',
                (since, namespace, namespace, max(1, min(int(limit), 100))),
            ).fetchall()

    def finding_history(self, fingerprint, limit=20):
        with self._pool.connection() as conn:
            return conn.execute(
                '''SELECT * FROM finding_observations WHERE fingerprint=%s
                   ORDER BY observed_at DESC,session_id DESC LIMIT %s''',
                (fingerprint, max(1, min(int(limit), 100))),
            ).fetchall()

    def claim_pending_notifications(self, now, limit=10):
        """Lease due work atomically; expired leases can be retried after a restart."""
        with self._pool.connection() as conn, conn.transaction():
            return conn.execute(
                '''WITH due AS (
                       SELECT id FROM notification_outbox
                       WHERE (status='pending' AND next_attempt_at <= %s)
                          OR (status='sending' AND lease_until <= %s)
                       ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT %s
                   ) UPDATE notification_outbox o
                     SET status='sending', attempts=attempts+1, lease_until=%s
                     FROM due WHERE o.id=due.id RETURNING o.id,o.payload,o.attempts''',
                (now, now, max(1, min(int(limit), 100)), now + timedelta(minutes=2)),
            ).fetchall()

    def mark_notification_delivered(self, notification_id, message_ts, now, *, attempts=None):
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE notification_outbox SET status='delivered', delivered_at=%s,
                   message_ts=%s,lease_until=NULL,last_error_type=NULL
                   WHERE id=%s AND status='sending' AND (%s::integer IS NULL OR attempts=%s)
                   RETURNING id""",
                (now, message_ts, notification_id, attempts, attempts),
            ).fetchone()
        return row is not None

    def reschedule_notification(self, notification_id, error_type, now, retry_after_seconds, *, attempts=None):
        safe_type = re.sub(r'[^A-Za-z0-9_]', '', str(error_type))[:80] or 'DeliveryError'
        delay = max(1, min(float(retry_after_seconds), 86400))
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE notification_outbox SET status='pending', next_attempt_at=%s,
                   lease_until=NULL,last_error_type=%s WHERE id=%s AND status='sending'
                   AND (%s::integer IS NULL OR attempts=%s) RETURNING id""",
                (now + timedelta(seconds=delay), safe_type, notification_id, attempts, attempts),
            ).fetchone()
        return row is not None

    def delivery_status(self):
        with self._pool.connection() as conn:
            row = conn.execute(
                '''SELECT count(*) FILTER (WHERE status='pending') AS pending,
                   count(*) FILTER (WHERE status='sending') AS in_flight,
                   min(created_at) FILTER (WHERE status <> 'delivered') AS oldest_pending_at,
                   max(delivered_at) AS last_delivered_at FROM notification_outbox''',
            ).fetchone()
        return {'available': True, **row}

    def mute_report_finding(self, report_id, fingerprint_hash, hours, forever=False):
        """Resolve an opaque Slack value only against its report's covered findings."""
        if hours is not None and type(hours) is not int:
            return False
        if (forever and hours not in (None, 0)) or (not forever and hours not in (0, 1, 8, 24, 168)):
            return False
        if not re.fullmatch(r'[a-f0-9]{64}', fingerprint_hash):
            return False
        now = datetime.now(timezone.utc)
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute('SELECT fingerprints FROM monitor_reports WHERE report_id=%s',
                               (report_id,)).fetchone()
            if not row:
                return False
            matches = [fp for fp in row['fingerprints']
                       if hashlib.sha256(fp.encode()).hexdigest() == fingerprint_hash]
            if len(matches) != 1:
                return False
            unignore = not forever and hours == 0
            until = None if forever else now + timedelta(hours=hours)
            row = conn.execute(
                '''UPDATE finding_state SET ignored_until=%s,ignored_forever=%s,
                   ack_until=CASE WHEN %s THEN NULL ELSE ack_until END
                   WHERE fingerprint=%s AND (resolved_at IS NULL OR %s) RETURNING fingerprint''',
                (until, forever, unignore, matches[0], unignore),
            ).fetchone()
            return row is not None

    def list_muted_findings(self, limit=50):
        with self._pool.connection() as conn:
            return conn.execute(
                '''SELECT f.fingerprint,f.namespace,f.kind,f.resource_name AS name,
                   f.reason,f.title,f.severity,f.ignored_until,f.ignored_forever,f.resolved_at,
                   (SELECT r.report_id FROM monitor_reports r
                    WHERE r.fingerprints ? f.fingerprint ORDER BY r.created_at DESC LIMIT 1) AS report_id
                   FROM finding_state f WHERE ignored_forever OR ignored_until > now()
                   ORDER BY f.last_seen DESC LIMIT %s''',
                (max(1, min(int(limit), 100)),),
            ).fetchall()

    # -- meta -------------------------------------------------------------

    def next_check_number(self) -> int:
        """Monotonic counter of completed checks, used to pace the digest."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO monitor_meta (key, value) VALUES ('check_count', '1')
                ON CONFLICT (key) DO UPDATE
                    SET value = (COALESCE(monitor_meta.value::bigint, 0) + 1)::text
                RETURNING value
                """
            )
            row = cur.fetchone()
        try:
            return int(row["value"])
        except (TypeError, ValueError, KeyError):
            return 0


def init_persistence(database_url: str = "") -> tuple[Any, Any, Any]:
    """Build ``(checkpointer, store, database)``.

    Falls back to in-memory implementations when no DSN is configured or the
    database cannot be reached, so the CLI and a degraded cluster both still work.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    if not database_url:
        log.warning(
            "DATABASE_URL not set — using in-memory checkpointer/store. Pending HITL "
            "approvals and monitoring finding-state will NOT survive a restart."
        )
        return MemorySaver(), InMemoryStore(), NullDatabase()

    pool = None
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore

        pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=10,
            open=True,
            # PostgresSaver requires both of these on every connection.
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

        checkpointer = PostgresSaver(pool)
        checkpointer.setup()

        store = PostgresStore(pool)
        store.setup()

        db = PostgresDatabase(pool)
        db.setup()

        log.info("Persistence ready (postgres)")
        return checkpointer, store, db
    except Exception:
        log.exception(
            "Postgres unavailable — DEGRADED: falling back to in-memory state. "
            "HITL approvals will not survive a restart and no audit trail is being written."
        )
        # An opened-but-unusable pool keeps background worker threads retrying
        # the dead DSN for the life of the process. Close it before degrading.
        if pool is not None:
            try:
                pool.close()
            except Exception:
                log.debug("Failed to close half-open pool", exc_info=True)
        return MemorySaver(), InMemoryStore(), NullDatabase()
