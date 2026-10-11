"""Durable monitoring, mute, and outbox contracts against real Postgres.

Set TEST_DATABASE_URL to an isolated test database. Tests truncate monitoring
state and never contact Slack or mutate a cluster.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

from monitor_state import diff_report, fingerprint, serialize_diff
from persistence import NullDatabase, PostgresDatabase, init_persistence
from schemas import CollectionCoverage, Finding, HealthReport

TEST_DSN = os.getenv("TEST_DATABASE_URL", "")
NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def db():
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    _, _, database = init_persistence(TEST_DSN)
    assert isinstance(database, PostgresDatabase), "TEST_DATABASE_URL must reach Postgres"
    yield database
    database.close()


@pytest.fixture(autouse=True)
def clean(request):
    if "db" not in request.fixturenames:
        yield
        return
    database = request.getfixturevalue("db")
    with database._pool.connection() as conn:
        conn.execute("TRUNCATE finding_state, monitor_reports, monitor_meta, monitor_checks, finding_observations, notification_outbox CASCADE")
    yield


def finding(namespace="prod", severity="critical"):
    return Finding(severity=severity, title="API Is Failing", detail="Container exits 1",
                   namespace=namespace, kind="Deployment", resource_name="api",
                   reason="CrashLoopBackOff")


def report(*findings, valid=True, coverage=None):
    return HealthReport(overall_severity="critical" if findings else "ok",
                        summary="Health check", findings=list(findings),
                        analysis_valid=valid, coverage=coverage or [])


def save(db, session, check_no, current, now=NOW, *, notify=True):
    diff = diff_report(current, db.load_tracked_findings(), now)
    notification = {"report": current.model_dump(mode="json"), "diff": serialize_diff(diff), "source": "scheduled"} if notify else None
    db.record_monitor_check(session, check_no, current, {}, diff, now, notification)
    return diff


def counts(db):
    with db._pool.connection() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                for table in ("finding_state", "monitor_reports", "monitor_checks", "finding_observations", "notification_outbox")}


def mute_identity(db, f):
    fp = fingerprint(f)
    return db.save_report([fp]), hashlib.sha256(fp.encode()).hexdigest(), fp


def test_monitor_write_is_atomic_and_session_retry_is_idempotent(db):
    f = finding()
    current = report(f)
    first_diff = diff_report(current, {}, NOW)
    notification = {"report": current.model_dump(mode="json"), "diff": serialize_diff(first_diff), "source": "scheduled"}
    db.record_monitor_check("check-one", 1, current, {}, first_diff, NOW, notification)
    assert counts(db) == {name: 1 for name in counts(db)}
    # Retry after a lost commit acknowledgement must not increment observations
    # or enqueue a second delivery, even if the caller reserved a new number.
    db.record_monitor_check("check-one", 2, current, {}, first_diff, NOW, notification)
    assert counts(db) == {name: 1 for name in counts(db)}
    assert db.load_tracked_findings()[fingerprint(f)].times_seen == 1
    pending = db.claim_pending_notifications(NOW + timedelta(seconds=1), limit=10)
    assert len(pending) == 1
    report_id = pending[0]["payload"]["report_id"]
    digest = hashlib.sha256(fingerprint(f).encode()).hexdigest()
    assert db.mute_report_finding(report_id, digest, 1)
    assert len(db.recent_monitor_checks(limit=10)) == 1
    assert len(db.finding_history(fingerprint(f), limit=10)) == 1


def test_invalid_json_rolls_back_the_entire_check(db):
    f = finding()
    current = report(f)
    diff = diff_report(current, {}, NOW)
    with pytest.raises(Exception):
        db.record_monitor_check("invalid-check", 1, current, {}, diff, NOW, {"not_json": object()})
    assert all(count == 0 for count in counts(db).values())
    assert db.load_tracked_findings() == {}


def test_delivery_claims_are_exclusive_and_delivered_rows_stay_closed(db):
    save(db, "delivery-one", 1, report(finding()))
    first = db.claim_pending_notifications(NOW + timedelta(seconds=1), limit=1)
    assert len(first) == 1 and first[0]["attempts"] == 1
    assert db.claim_pending_notifications(NOW + timedelta(seconds=2), limit=1) == []
    db.mark_notification_delivered(first[0]["id"], "123.456", NOW + timedelta(seconds=3))
    assert db.claim_pending_notifications(NOW + timedelta(days=1), limit=10) == []


def test_retry_due_time_and_stale_lease_recovery(db):
    save(db, "delivery-one", 1, report(finding()))
    start = NOW + timedelta(seconds=1)
    first = db.claim_pending_notifications(start, limit=1)[0]
    db.reschedule_notification(first["id"], "RateLimited", start, 120)
    assert db.claim_pending_notifications(start + timedelta(seconds=119), limit=1) == []
    retry = db.claim_pending_notifications(start + timedelta(seconds=121), limit=1)[0]
    assert retry["id"] == first["id"] and retry["attempts"] == 2
    # A crashed delivery worker's lease must eventually become claimable.
    recovered = db.claim_pending_notifications(start + timedelta(days=1), limit=1)[0]
    assert recovered["id"] == first["id"] and recovered["attempts"] == 3


def test_claim_skips_a_row_locked_by_another_worker(db):
    save(db, "delivery-one", 1, report(finding()))
    with db._pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT id FROM notification_outbox FOR UPDATE").fetchall()
            assert db.claim_pending_notifications(NOW + timedelta(seconds=1), limit=10) == []
    assert len(db.claim_pending_notifications(NOW + timedelta(seconds=2), limit=10)) == 1


@pytest.mark.parametrize("hours", [1, 8, 24, 168])
def test_ignore_periods_cover_only_the_selected_report_finding(db, hours):
    f, other = finding(), finding(namespace="staging")
    save(db, "initial", 1, report(f, other), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    before = datetime.now(timezone.utc)
    assert db.mute_report_finding(report_id, digest, hours)
    state = db.load_tracked_findings()
    assert before + timedelta(hours=hours) <= state[fp].ignored_until <= datetime.now(timezone.utc) + timedelta(hours=hours)
    assert state[fp].ignored_forever is False
    assert state[fingerprint(other)].ignored_until is None
    assert len(db.list_muted_findings()) == 1
    assert not db.mute_report_finding(report_id, hashlib.sha256(fingerprint(other).encode()).hexdigest(), hours)
    assert not db.mute_report_finding("missing-report", digest, hours)


@pytest.mark.parametrize("hours,forever", [(2, False), (-1, False), (None, False), (1, True)])
def test_unsupported_mute_parameters_are_rejected(db, hours, forever):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    assert not db.mute_report_finding(report_id, digest, hours, forever=forever)
    assert not db.load_tracked_findings()[fp].is_ignored(datetime.now(timezone.utc))


def test_expired_ignore_alerts_once_and_clears_expiry_marker(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    assert db.mute_report_finding(report_id, digest, 1)
    with db._pool.connection() as conn:
        conn.execute("UPDATE finding_state SET ignored_until = %s WHERE fingerprint = %s", (NOW - timedelta(seconds=1), fp))
    assert db.load_tracked_findings()[fp].mute_expired is True
    expired = save(db, "after-expiry", 2, report(f), NOW + timedelta(minutes=1))
    assert expired.should_notify()
    assert not db.load_tracked_findings()[fp].mute_expired
    ongoing = diff_report(report(f), db.load_tracked_findings(), NOW + timedelta(minutes=2))
    assert not ongoing.should_notify()


def test_forever_survives_database_wrapper_restart_resolution_and_recurrence(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    assert db.mute_report_finding(report_id, digest, None, forever=True)
    restarted = PostgresDatabase(db._pool)
    assert restarted.load_tracked_findings()[fp].ignored_forever
    save(restarted, "resolved", 2, report(), NOW + timedelta(minutes=1), notify=False)
    assert restarted.load_tracked_findings()[fp].resolved_at is not None
    recurrence = save(restarted, "recurred", 3, report(f), NOW + timedelta(minutes=2), notify=False)
    assert not recurrence.should_notify()
    assert restarted.load_tracked_findings()[fp].ignored_forever
    assert restarted.load_tracked_findings()[fp].resolved_at is None


def test_unignore_clears_report_ack_and_realerts_next_check(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    assert db.ack_report(report_id, 8) == 1
    assert db.mute_report_finding(report_id, digest, None, forever=True)
    assert db.mute_report_finding(report_id, digest, 0)
    state = db.load_tracked_findings()[fp]
    assert state.ack_until is None and not state.ignored_forever
    assert state.mute_expired
    assert diff_report(report(f), {fp: state}, NOW + timedelta(minutes=1)).should_notify()


def test_unknown_collection_retains_state_and_records_history(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    incomplete = report(valid=False, coverage=[CollectionCoverage(area="pods", status="unavailable")])
    incomplete.overall_severity = "unknown"
    unknown = save(db, "unknown", 2, incomplete, NOW + timedelta(minutes=1), notify=False)
    assert not unknown.resolved
    assert db.load_tracked_findings()[fingerprint(f)].resolved_at is None
    checks = db.recent_monitor_checks(limit=10)
    assert len(checks) == 2
    assert checks[0]["analysis_valid"] is False
    history = db.finding_history(fingerprint(f), limit=10)
    assert any(row["status"] == "retained" for row in history)


def test_history_namespace_filter_and_time_window(db):
    prod, stage = finding(), finding(namespace="staging")
    save(db, "prod-check", 1, report(prod), NOW, notify=False)
    save(db, "staging-check", 2, report(stage), NOW + timedelta(minutes=1), notify=False)
    prod_checks = db.recent_monitor_checks(namespace="prod", limit=10)
    assert {row["session_id"] for row in prod_checks} == {"prod-check", "staging-check"}
    assert db.recent_monitor_checks(namespace="unrelated", limit=10) == []
    assert [row["session_id"] for row in db.recent_monitor_checks(since=NOW + timedelta(seconds=30), limit=10)] == ["staging-check"]
    history = db.finding_history(fingerprint(prod), limit=10)
    assert {row["status"] for row in history} >= {"new", "resolved"}


def test_null_database_monitoring_contract_is_unavailable_and_safe():
    db = NullDatabase()
    current = report(finding())
    assert not db.available
    assert db.record_monitor_check("unavailable", 1, current, {}, diff_report(current, {}, NOW), NOW, {}) is None
    assert db.recent_monitor_checks() == []
    assert db.finding_history("missing") == []
    assert db.claim_pending_notifications(NOW) == []
    assert not db.mute_report_finding("report", "0" * 64, 1)
    assert db.list_muted_findings() == []
    assert db.mark_notification_delivered("missing", "1.2", NOW) is None
    assert db.reschedule_notification("missing", "Unavailable", NOW, 15) is None


def test_unknown_check_after_mute_expiry_preserves_realert_until_confirmed(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    assert db.mute_report_finding(report_id, digest, 1)
    with db._pool.connection() as conn:
        conn.execute("UPDATE finding_state SET ignored_until=%s WHERE fingerprint=%s", (NOW - timedelta(seconds=1), fp))
    unknown = report(valid=False, coverage=[CollectionCoverage(area="pods", status="unavailable")])
    unknown.overall_severity = "unknown"
    save(db, "unknown-after-expiry", 2, unknown, NOW + timedelta(minutes=1), notify=False)
    assert db.load_tracked_findings()[fp].mute_expired
    confirmed = save(db, "confirmed-after-expiry", 3, report(f), NOW + timedelta(minutes=2), notify=False)
    assert confirmed.should_notify()
    assert not db.load_tracked_findings()[fp].mute_expired


def test_history_retention_keeps_pending_delivery_and_its_report_reference(db):
    f = finding()
    old = NOW - timedelta(days=31)
    save(db, "old-delivered", 1, report(f), old)
    delivered = db.claim_pending_notifications(old + timedelta(seconds=1), limit=1)[0]
    db.mark_notification_delivered(delivered["id"], "123.456", old + timedelta(seconds=2))
    save(db, "old-pending", 2, report(f), old + timedelta(minutes=1))
    save(db, "current", 3, report(f), NOW, notify=False)
    checks = db.recent_monitor_checks(since=old - timedelta(minutes=1), limit=100)
    assert [row["session_id"] for row in checks] == ["current"]
    assert len(db.finding_history(fingerprint(f), limit=100)) == 1
    pending = db.claim_pending_notifications(NOW, limit=10)
    assert len(pending) == 1
    report_id = pending[0]["payload"]["report_id"]
    digest = hashlib.sha256(fingerprint(f).encode()).hexdigest()
    assert db.mute_report_finding(report_id, digest, 1)
    with db._pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM notification_outbox").fetchone()["count"] == 1


def test_resolved_findings_cannot_be_muted_but_can_be_unignored(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, digest, fp = mute_identity(db, f)
    save(db, "resolved", 2, report(), NOW + timedelta(minutes=1), notify=False)
    assert db.load_tracked_findings()[fp].resolved_at is not None
    assert not db.mute_report_finding(report_id, digest, 8)
    assert not db.mute_report_finding(report_id, digest, None, forever=True)
    assert db.mute_report_finding(report_id, digest, 0)


def test_malformed_fingerprint_hash_is_rejected_without_state_change(db):
    f = finding()
    save(db, "initial", 1, report(f), notify=False)
    report_id, _, fp = mute_identity(db, f)
    for bad_hash in (fp, "0" * 63, "A" * 64, "0" * 64 + "\n"):
        assert not db.mute_report_finding(report_id, bad_hash, 1)
    assert db.load_tracked_findings()[fp].ignored_until is None


def test_reclaimed_lease_fences_stale_worker_completion_and_retry(db):
    save(db, "delivery-one", 1, report(finding()))
    first = db.claim_pending_notifications(NOW + timedelta(seconds=1), limit=1)[0]
    reclaimed_at = NOW + timedelta(minutes=3)
    fresh = db.claim_pending_notifications(reclaimed_at, limit=1)[0]
    assert fresh["id"] == first["id"] and fresh["attempts"] > first["attempts"]
    assert not db.mark_notification_delivered(first["id"], "stale.1", reclaimed_at, attempts=first["attempts"])
    assert not db.reschedule_notification(first["id"], "StaleWorker", reclaimed_at, 15, attempts=first["attempts"])
    # Failed stale operations must leave the fresh worker's claim intact.
    assert db.claim_pending_notifications(reclaimed_at + timedelta(seconds=16), limit=1) == []
    assert db.mark_notification_delivered(fresh["id"], "fresh.2", reclaimed_at + timedelta(seconds=17), attempts=fresh["attempts"])
    assert db.claim_pending_notifications(reclaimed_at + timedelta(days=1), limit=1) == []
