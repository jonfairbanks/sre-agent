"""Crash evidence recovery survives worker restarts and updates one message."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from crash_evidence_delivery import enrich_crash_payload, evidence_retry_delay
from crash_watcher import CrashEvidence
from notification_delivery import NotificationDelivery
from slack_notifier import SlackNotifier


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
STATE = {
    "uid": "original", "namespace": "apps", "pod_name": "worker", "container": "main",
    "now": NOW.isoformat(), "expected_finished_at": NOW.isoformat(),
    "logs_available": False, "events_available": True, "capture_count": 0,
    "summary": "Pod worker crashed.",
    "sections": {"termination": "Termination: Error (exit 1).",
                 "logs": "Recent Logs: unavailable.", "events": "Recent Warning Events:\nUnhealthy: probe failed"},
}
PAYLOAD = {"type": "crash_alert", "severity": "critical", "title": "Pod Crash Detected",
           "namespace": "apps", "message": "initial evidence", "crash_evidence": STATE}


def capture(*, logs=True, events=False, retryable=True):
    evidence = CrashEvidence("new capture", logs_available=logs, events_available=events,
                             retryable=retryable, sections={
                                 "logs": "Recent Logs:\n```\nfatal: lost lease\n```" if logs else "Recent Logs: unavailable.",
                                 "events": "Recent Warning Events: none in crash window." if events else "Recent Warning Events: unavailable.",
                             })
    evidence.expected_finished_at = NOW.isoformat()
    return evidence


class Queue:
    def __init__(self):
        self.payload = deepcopy(PAYLOAD)
        self.due = NOW
        self.attempts = 0
        self.delivered = False

    def claim_pending_notifications(self, now, limit=1):
        if self.delivered or now < self.due:
            return []
        self.attempts += 1
        self.due = now.replace(year=2027)
        return [{"id": "notification-id", "payload": deepcopy(self.payload), "attempts": self.attempts}]

    def defer_crash_evidence(self, notification_id, payload, now, delay, *, attempts):
        from datetime import timedelta
        assert attempts == self.attempts
        self.payload = deepcopy(payload)
        self.due = now + timedelta(seconds=delay)
        return True

    def mark_notification_delivered(self, notification_id, message_ts, now, *, attempts):
        assert attempts == self.attempts
        self.delivered = True
        return True

    def save_crash_evidence_payload(self, notification_id, payload, *, attempts):
        assert attempts == self.attempts
        self.payload = deepcopy(payload)
        return True

    def reschedule_notification(self, notification_id, error, now, delay, *, attempts):
        from datetime import timedelta
        self.due = now + timedelta(seconds=delay)


def test_recovered_logs_keep_earlier_events_and_original_crash_identity():
    collector = Mock(return_value=capture())
    updated, pending = enrich_crash_payload(PAYLOAD, collector)
    assert not pending
    assert "fatal: lost lease" in updated["message"]
    assert "Unhealthy: probe failed" in updated["message"]
    assert collector.call_args.kwargs["now"] == NOW
    assert collector.call_args.kwargs["expected_finished_at"] == NOW.isoformat()
    assert PAYLOAD["crash_evidence"]["logs_available"] is False


def test_initial_alert_then_restarted_worker_updates_original_message():
    db = Queue()
    notifier = Mock(enabled=True)
    notifier.send_alert.return_value = "1.2"
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 1
    assert not db.delivered
    assert db.payload["type"] == "crash_evidence"
    assert db.payload["message_ts"] == "1.2"
    with patch("notification_delivery.enrich_crash_payload", side_effect=lambda p: enrich_crash_payload(p, Mock(return_value=capture()))):
        assert NotificationDelivery(db, notifier, clock=lambda now=db.due: now).drain_once() == 1
    assert db.delivered
    assert notifier.send_alert.call_count == 2
    initial, update = notifier.send_alert.call_args_list
    assert "message_ts" not in initial.kwargs
    assert update.kwargs["message_ts"] == "1.2"
    assert "fatal: lost lease" in update.args[2]


def test_incomplete_capture_remains_durable_with_capped_retry_delay():
    db = Queue()
    notifier = Mock(enabled=True)
    notifier.send_alert.return_value = "1.2"
    NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once()
    with patch("notification_delivery.enrich_crash_payload", side_effect=lambda p: enrich_crash_payload(p, Mock(return_value=capture(logs=False)))):
        NotificationDelivery(db, notifier, clock=lambda now=db.due: now).drain_once()
    assert not db.delivered
    assert db.payload["crash_evidence"]["capture_count"] == 1
    assert "Unhealthy: probe failed" in db.payload["message"]
    db.payload["crash_evidence"]["capture_count"] = 10000
    assert evidence_retry_delay(db.payload) == 300


def test_failed_slack_update_retries_same_message_after_restart():
    db = Queue()
    notifier = Mock(enabled=True)
    notifier.send_alert.return_value = "1.2"
    NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once()
    notifier.send_alert.side_effect = [TimeoutError(), "1.2"]
    collector = Mock(side_effect=[capture(), AssertionError("must retain captured evidence")])
    with patch("notification_delivery.enrich_crash_payload", side_effect=lambda p: enrich_crash_payload(p, collector)):
        assert NotificationDelivery(db, notifier, clock=lambda now=db.due: now).drain_once() == 0
        assert "fatal: lost lease" in db.payload["message"]
        assert NotificationDelivery(db, notifier, clock=lambda now=db.due: now).drain_once() == 1
    collector.assert_called_once()
    assert db.delivered
    assert all(call.kwargs["message_ts"] == "1.2" for call in notifier.send_alert.call_args_list[1:])


def test_original_termination_and_observed_warning_survive_later_empty_capture():
    later = capture(logs=False, events=True, retryable=False)
    later.sections["termination"] = "Termination: OOMKilled (exit 137)."
    updated, pending = enrich_crash_payload(PAYLOAD, Mock(return_value=later))
    assert not pending
    assert "Termination: Error (exit 1)." in updated["message"]
    assert "OOMKilled" not in updated["message"]
    assert "Unhealthy: probe failed" in updated["message"]


def test_canonical_channel_survives_notifier_restart_and_config_change():
    db = Queue()
    first = Mock(enabled=True, _channel_id="C_ORIGINAL")
    first.send_alert.return_value = "1.2"
    NotificationDelivery(db, first, clock=lambda: NOW).drain_once()
    assert db.payload["message_channel"] == "C_ORIGINAL"
    restarted = SlackNotifier(bot_token="test", channel="#different-channel")
    restarted._client = Mock()
    restarted._client.chat_update.return_value = {"ts": "1.2", "channel": "C_ORIGINAL"}
    with patch("notification_delivery.enrich_crash_payload", side_effect=lambda p: enrich_crash_payload(p, Mock(return_value=capture()))):
        NotificationDelivery(db, restarted, clock=lambda now=db.due: now).drain_once()
    restarted._client.chat_postMessage.assert_not_called()
    assert restarted._client.chat_update.call_args.kwargs["channel"] == "C_ORIGINAL"


def test_partial_warning_captures_are_merged():
    later = capture(events=True)
    later.sections["events"] = "Recent Warning Events:\nBackOff: restarting failed container"
    updated, _ = enrich_crash_payload(PAYLOAD, Mock(return_value=later))
    assert "Unhealthy: probe failed" in updated["message"]
    assert "BackOff: restarting failed container" in updated["message"]


def test_alert_update_uses_slack_update_without_posting():
    notifier = SlackNotifier(bot_token="test", channel="C1")
    notifier._client = Mock()
    notifier._client.chat_update.return_value = {"ts": "1.2", "channel": "C1"}
    assert notifier.send_alert("critical", "Pod Crash Detected", "recovered logs",
                               message_ts="1.2", notification_id="stable-id", raise_on_error=True) == "1.2"
    notifier._client.chat_postMessage.assert_not_called()
    assert notifier._client.chat_update.call_args.kwargs["ts"] == "1.2"
    assert "client_msg_id" not in notifier._client.chat_update.call_args.kwargs
