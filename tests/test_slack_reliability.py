"""Slack retries and safe, independent inbound and outbound diagnostics."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from notification_delivery import NotificationDelivery
from schemas import HealthReport
from slack_health import (SlackRuntimeHealth, SafeSocketLogger, SlackDeliveryError,
                          delivery_error, install_socket_health)
from slack_notifier import SlackNotifier

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


class QueueDB:
    def __init__(self):
        self.row = {"id": "5d581dbf-274f-4567-9c45-c70bbd6c4e67", "attempts": 0,
                    "payload": {"report": {"overall_severity": "ok", "summary": "Healthy"}}}
        self.due = NOW
        self.delivered = False
        self.errors = []

    def claim_pending_notifications(self, now, limit):
        if self.delivered or now < self.due:
            return []
        self.row["attempts"] += 1
        self.due = now + timedelta(minutes=5)
        return [dict(self.row)]

    def mark_notification_delivered(self, notification_id, message_ts, now, *, attempts):
        assert attempts == self.row["attempts"]
        self.delivered = True
        self.message_ts = message_ts
        return True

    def reschedule_notification(self, notification_id, error_type, now, retry_after_seconds, *, attempts):
        assert attempts == self.row["attempts"]
        self.errors.append(error_type)
        self.due = now + timedelta(seconds=retry_after_seconds)


def test_retry_survives_service_restart_and_delivered_notification_is_not_resent():
    db = QueueDB()
    notifier = Mock(enabled=True)
    notifier.send_structured_report.side_effect = [SlackDeliveryError("BrokenPipeError"), "1.2"]
    now = [NOW]
    delivery = NotificationDelivery(db, notifier, clock=lambda: now[0])
    assert delivery.drain_once() == 0
    assert db.errors == ["BrokenPipeError"]
    now[0] += timedelta(seconds=14)
    assert delivery.drain_once() == 0
    now[0] += timedelta(seconds=1)
    restarted = NotificationDelivery(db, notifier, clock=lambda: now[0])
    assert restarted.drain_once() == 1
    assert restarted.drain_once() == 0
    assert notifier.send_structured_report.call_count == 2
    assert all(call.kwargs["notification_id"] == db.row["id"] for call in notifier.send_structured_report.call_args_list)


def test_rate_limit_honors_retry_after():
    db = QueueDB()
    notifier = Mock(enabled=True)
    notifier.send_structured_report.side_effect = SlackDeliveryError("SlackApiError", 120)
    NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once()
    assert db.due == NOW + timedelta(seconds=120)


def test_disabled_notifier_does_not_claim_rows():
    db = Mock()
    NotificationDelivery(db, Mock(enabled=False)).drain_once()
    db.claim_pending_notifications.assert_not_called()


def test_drain_loop_stops_cleanly():
    async def run():
        db = QueueDB()
        notifier = Mock(enabled=True)
        notifier.send_structured_report.return_value = "1.2"
        delivery = NotificationDelivery(db, notifier, clock=lambda: NOW, retry_interval_seconds=.01)
        await delivery.start()
        for _ in range(100):
            if db.delivered:
                break
            await asyncio.sleep(.001)
        await delivery.stop()
        assert db.delivered
        assert delivery._task is None
    asyncio.run(run())


def test_inbound_disconnect_does_not_change_outbound_health():
    health = SlackRuntimeHealth()
    health.outbound_enabled(True)
    health.outbound_result()
    client = SimpleNamespace(on_message_listeners=[], on_error_listeners=[], on_close_listeners=[],
                             is_connected=lambda: False)
    install_socket_health(SimpleNamespace(client=client), health)
    client.on_message_listeners[0]('{"type":"hello","token":"secret"}')
    client.on_error_listeners[0](BrokenPipeError("xapp-secret"))
    client.on_close_listeners[0](1006, "private payload")
    status = health.status()
    assert status["socket"]["state"] == "disconnected"
    assert status["socket"]["last_error_type"] == "BrokenPipeError"
    assert status["outbound"]["state"] == "healthy"
    assert status["outbound"]["successes"] == 1
    assert "secret" not in str(status)
    client.is_connected = lambda: True
    assert health.status()["socket"]["state"] == "connected"


def test_sdk_logging_retains_safe_frames_without_tokens_or_payload(caplog):
    health = SlackRuntimeHealth()
    logger = SafeSocketLogger(health)
    with caplog.at_level(logging.INFO):
        try:
            raise BrokenPipeError("xapp-secret wss://private payload body")
        except BrokenPipeError:
            logger.exception("session token=%s", "xoxb-secret")
    assert "Slack SDK socket_error" in caplog.text
    assert "secret" not in caplog.text
    assert "payload" not in caplog.text
    assert "Traceback" not in caplog.text
    status = health.status()["socket"]
    assert status["last_error_type"] == "BrokenPipeError"
    assert status["last_error_frames"][-1]["function"] == "test_sdk_logging_retains_safe_frames_without_tokens_or_payload"
    assert set(status["last_error_frames"][-1]) == {"module", "function", "line"}


def test_notifier_stable_id_and_safe_error_preserve_compatibility(caplog):
    notifier = SlackNotifier("", "C1")
    notifier._client = Mock()
    notifier._client.chat_postMessage.side_effect = BrokenPipeError("xoxb-secret report payload")
    report = HealthReport(overall_severity="ok", summary="Healthy")
    assert notifier.send_structured_report(report) is None
    with pytest.raises(SlackDeliveryError) as raised:
        notifier.send_structured_report(report, notification_id="uuid", raise_on_error=True)
    assert raised.value.error_type == "BrokenPipeError"
    assert notifier._client.chat_postMessage.call_args.kwargs["client_msg_id"] == "uuid"
    assert "secret" not in caplog.text
    assert "payload" not in caplog.text
    assert notifier.health.status()["outbound"]["failures"] == 2


def test_retry_after_extracted_without_response_contents():
    error = RuntimeError("secret")
    error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "47"})
    assert delivery_error(error).retry_after_seconds == 47


def test_unverified_incident_does_not_render_all_clear_or_recovered():
    from monitor_state import ReportDiff, StoredFinding
    notifier = SlackNotifier("", "C1")
    notifier._client = Mock()
    notifier._client.chat_postMessage.return_value = {"ts": "1.2"}
    retained = StoredFinding(fingerprint="pod:prod:api:NotReady", severity="critical",
                             title="API Pod Not Ready", namespace="prod", first_seen=NOW,
                             last_seen=NOW, times_seen=1)
    diff = ReportDiff(retained=[retained])
    report = HealthReport(overall_severity="unknown", summary="Pod collection unavailable",
                          coverage=[{"area": "pods", "status": "unavailable"}])
    notifier.send_structured_report(report, diff=diff)
    payload = notifier._client.chat_postMessage.call_args.kwargs
    assert "Health Unknown" in payload["text"]
    assert "All Clear" not in payload["text"]
    assert "Recovered" not in payload["text"]
    assert "Unverified Findings" in str(payload["blocks"])
    assert "API Pod Not Ready" in str(payload["blocks"])
    assert "Collection Coverage" in str(payload["blocks"])


def test_durable_report_has_per_finding_ignore_and_bulk_ack_controls():
    from monitor_state import diff_report
    report = HealthReport(overall_severity="critical", summary="Issues found", findings=[
        {"severity": "critical", "title": "Pod Not Ready", "detail": "readiness fails",
         "kind": "Pod", "namespace": "prod", "resource_name": "api", "reason": "NotReady"}])
    notifier = SlackNotifier("", "C1")
    notifier._client = Mock()
    notifier._client.chat_postMessage.return_value = {"ts": "1.2"}
    notifier.send_structured_report(report, diff=diff_report(report, {}, NOW), report_id="report1")
    payload = notifier._client.chat_postMessage.call_args.kwargs
    accessory = payload["attachments"][0]["blocks"][0]["accessory"]
    assert accessory["action_id"] == "sre_ignore_finding"
    assert "Forever" in str(accessory)
    assert "sre_ack" in str(payload)


def test_stale_lease_cannot_count_delivery_as_complete():
    db = Mock()
    db.claim_pending_notifications.return_value = [{"id": "uuid", "attempts": 2,
        "payload": {"report": {"overall_severity": "ok", "summary": "Healthy"}}}]
    db.mark_notification_delivered.return_value = False
    notifier = Mock(enabled=True)
    notifier.send_structured_report.return_value = "1.2"
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 0
    db.mark_notification_delivered.assert_called_once_with("uuid", "1.2", NOW, attempts=2)
    db.reschedule_notification.assert_not_called()


@pytest.mark.parametrize("other_visible", [False, True])
def test_queued_alert_filters_later_ignore_without_changing_history(other_visible):
    from monitor_state import StoredFinding, diff_report, serialize_diff
    from schemas import Finding
    report = HealthReport(overall_severity="critical", summary="Issues found", findings=[
        Finding(severity="critical", title="Ignored Issue", detail="failed", kind="Pod", resource_name="api", reason="NotReady"),
        *([Finding(severity="critical", title="Visible Issue", detail="failed", kind="Pod", resource_name="worker", reason="NotReady")] if other_visible else []),
    ])
    diff = diff_report(report, {}, NOW)
    original = serialize_diff(diff)
    ignored = StoredFinding(fingerprint=diff.new[0].fingerprint, severity="critical", title="Ignored Issue",
        namespace="", first_seen=NOW, last_seen=NOW, times_seen=1, ignored_forever=True)
    db = QueueDB()
    db.row["payload"] = {"report": report.model_dump(mode="json"), "diff": original, "source": "scheduled"}
    db.load_tracked_findings = lambda: {ignored.fingerprint: ignored}
    notifier = Mock(enabled=True)
    notifier.send_structured_report.return_value = "1.2"
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 1
    if other_visible:
        sent_diff = notifier.send_structured_report.call_args.kwargs["diff"]
        assert [d.finding.title for d in sent_diff.active] == ["Visible Issue"]
        assert [d.finding.title for d in sent_diff.suppressed] == ["Ignored Issue"]
    else:
        notifier.send_structured_report.assert_not_called()
        assert db.message_ts is None
    assert db.row["payload"]["diff"] == original


def test_current_mute_read_failure_defers_without_sending():
    from monitor_state import diff_report, serialize_diff
    report = HealthReport(overall_severity="ok", summary="Healthy")
    db = QueueDB()
    db.row["payload"] = {"report": report.model_dump(mode="json"),
                          "diff": serialize_diff(diff_report(report, {}, NOW)), "source": "scheduled digest"}
    db.load_tracked_findings = Mock(side_effect=RuntimeError("credential-token"))
    notifier = Mock(enabled=True)
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 0
    notifier.send_structured_report.assert_not_called()
    assert db.errors == ["RuntimeError"]
