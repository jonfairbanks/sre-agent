"""Drain durable notifications independently of the monitoring interval.

Stable client_msg_id values help Slack identify retries. A failure after Slack
accepts a post can still produce duplicates; this is at-least-once delivery.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
from monitor_state import deserialize_diff
from schemas import HealthReport
from slack_health import SlackDeliveryError, safe_error_type

log = logging.getLogger("sre-agent.notifications")


class NotificationDelivery:
    def __init__(self, db, notifier, *, clock=None, retry_interval_seconds=15):
        self.db = db
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retry_interval_seconds = retry_interval_seconds
        self._task = None
        self._stop = asyncio.Event()

    def drain_once(self):
        if not self.notifier.enabled:
            return 0
        delivered = 0
        # Lease each row immediately before its HTTP request. Leasing a batch
        # upfront lets later rows expire while earlier posts are still running.
        for _ in range(10):
            rows = self.db.claim_pending_notifications(self.clock(), limit=1)
            if not rows:
                break
            row = rows[0]
            try:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                report = HealthReport.model_validate(payload["report"])
                diff = deserialize_diff(payload["diff"]) if payload.get("diff") is not None else None
                muted_any = False
                if diff is not None:
                    current = self.db.load_tracked_findings()
                    now = self.clock()
                    def muted(fingerprint):
                        finding = current.get(fingerprint)
                        return finding is not None and (finding.is_ignored(now) or finding.is_acked(now))
                    for bucket in ("new", "escalated", "ongoing"):
                        visible = []
                        for delta in getattr(diff, bucket):
                            if muted(delta.fingerprint):
                                diff.suppressed.append(delta)
                                muted_any = True
                            else:
                                visible.append(delta)
                        setattr(diff, bucket, visible)
                    for resolved in diff.resolved:
                        if muted(resolved.fingerprint):
                            resolved.suppressed = True
                            muted_any = True
                    diff.retained = [item for item in diff.retained if not muted(item.fingerprint)]
                unknown = (report.overall_severity == "unknown" or not report.analysis_valid
                           or any(c.status != "complete" for c in report.coverage))
                # A mute can be pressed on an earlier report while this row is
                # retrying. Retire it without sending an outdated alert.
                if (payload.get("source", "scheduled") == "scheduled" and muted_any
                        and not diff.should_notify() and not unknown and not diff.retained):
                    if not self.db.mark_notification_delivered(
                        row["id"], None, self.clock(), attempts=row["attempts"],
                    ):
                        break
                    delivered += 1
                    continue
                message_ts = self.notifier.send_structured_report(
                    report,
                    source=payload.get("source", "scheduled"),
                    channel=payload.get("channel"), thread_ts=payload.get("thread_ts"),
                    diff=diff,
                    report_id=payload.get("report_id"),
                    recovered_probe_events=payload.get("recovered_probe_events"),
                    notification_id=row["id"], raise_on_error=True,
                )
                if not message_ts:
                    raise SlackDeliveryError("NoMessageTimestamp")
                if not self.db.mark_notification_delivered(
                    row["id"], message_ts, self.clock(), attempts=row["attempts"],
                ):
                    # Another worker owns the recovered lease. Do not change
                    # its state or make another delivery attempt in this pass.
                    break
                delivered += 1
            except Exception as error:
                attempts = min(max(int(row.get("attempts", 1)), 1), 10)
                delay = min(15 * 2 ** (attempts - 1), 3600)
                if isinstance(error, SlackDeliveryError):
                    error_type = error.error_type
                    retry_after = error.retry_after_seconds
                    if retry_after is not None and math.isfinite(retry_after):
                        delay = max(delay, retry_after)
                else:
                    error_type = safe_error_type(error)
                self.db.reschedule_notification(row["id"], error_type, self.clock(), delay, attempts=row["attempts"])
                log.warning("Notification delivery deferred (%s)", error_type)
        return delivered

    async def _run(self):
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.drain_once)
            except Exception as error:
                log.warning("Notification drain failed (%s)", safe_error_type(error))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.retry_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
