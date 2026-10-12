"""Queued alerts honor migrated mutes and avoid false healthy headlines."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from monitor_state import StoredFinding, diff_report, fingerprint, serialize_diff
from notification_delivery import NotificationDelivery
from schemas import Finding, HealthReport
from slack_notifier import SlackNotifier

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


class PendingDB:
    def __init__(self, report, diff, current, aliases):
        self.row = {"id": "queued", "attempts": 1, "payload": {
            "report": report.model_dump(mode="json"), "diff": serialize_diff(diff), "source": "scheduled"}}
        self.current = current
        self.aliases = aliases
        self.delivered = False
        self.error = None

    def claim_pending_notifications(self, now, limit):
        return [] if self.delivered or self.error else [self.row]

    def load_finding_aliases(self):
        return self.aliases

    def load_tracked_findings(self):
        return self.current

    def mark_notification_delivered(self, notification_id, message_ts, now, *, attempts):
        self.delivered = True
        return True

    def reschedule_notification(self, notification_id, error_type, now, delay, *, attempts):
        self.error = error_type


def filesystem_finding(name):
    return Finding(severity="warning", title="Local Filesystem Usage", detail="Filesystem is 78% full.",
                   kind="Node", resource_name=name, reason="LocalFilesystemUsage")


@pytest.mark.parametrize("other_visible", [False, True])
@pytest.mark.parametrize("mute", ["ignored", "acknowledged"])
def test_pre_migration_queued_alert_honors_canonical_mute(other_visible, mute):
    old = filesystem_finding("k2:path:/data")
    canonical = fingerprint(filesystem_finding("k2:device:/dev/data"))
    visible = filesystem_finding("k3:device:/dev/data")
    report = HealthReport(overall_severity="warning", summary="Filesystem pressure.",
                          findings=[old, *([visible] if other_visible else [])])
    diff = diff_report(report, {}, NOW)
    current = StoredFinding(fingerprint=canonical, severity="warning", title=old.title,
                            namespace="", first_seen=NOW, last_seen=NOW, times_seen=1,
                            ignored_forever=mute == "ignored",
                            ack_until=NOW + timedelta(hours=1) if mute == "acknowledged" else None)
    db = PendingDB(report, diff, {canonical: current}, {fingerprint(old): "intermediate", "intermediate": canonical})
    original = serialize_diff(diff)
    notifier = Mock(enabled=True)
    notifier.send_structured_report.return_value = "1.2"
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 1
    if other_visible:
        sent = notifier.send_structured_report.call_args.kwargs["diff"]
        assert [delta.fingerprint for delta in sent.active] == [fingerprint(visible)]
        assert [delta.fingerprint for delta in sent.suppressed] == [fingerprint(old)]
    else:
        notifier.send_structured_report.assert_not_called()
    assert db.row["payload"]["diff"] == original


def test_alias_read_failure_defers_delivery_without_sending():
    report = HealthReport(overall_severity="warning", summary="Pressure.", findings=[filesystem_finding("k2:/data")])
    db = PendingDB(report, diff_report(report, {}, NOW), {}, {})
    db.load_finding_aliases = Mock(side_effect=RuntimeError("private-token"))
    notifier = Mock(enabled=True)
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 0
    assert db.error == "RuntimeError"
    notifier.send_structured_report.assert_not_called()


@pytest.mark.parametrize("uncertain", [False, True])
def test_suppressed_only_report_names_ignored_issues_without_claiming_health(uncertain):
    finding = filesystem_finding("k2:/data")
    report = HealthReport(overall_severity="warning", summary="1 active finding: warning.", findings=[finding],
                          analysis_valid=not uncertain,
                          coverage=[{"area": "pods", "status": "unavailable" if uncertain else "complete"}])
    state = StoredFinding(fingerprint=fingerprint(finding), severity=finding.severity, title=finding.title,
                          namespace="", first_seen=NOW, last_seen=NOW, times_seen=1, ignored_forever=True)
    diff = diff_report(report, {state.fingerprint: state}, NOW)
    notifier = SlackNotifier("", "C1")
    notifier._client = Mock()
    notifier.send_structured_report(report, diff=diff)
    payload = notifier._client.chat_postMessage.call_args.kwargs
    assert "All Clear" not in payload["text"]
    assert ":large_green_circle:" not in payload["text"]
    if uncertain:
        assert "Health Unknown" in payload["text"]
        assert "Issues Ignored" not in payload["text"]
        assert payload["blocks"][1]["text"]["text"] == report.summary
    else:
        assert "Issues Ignored" in payload["text"]
        assert ":large_yellow_circle:" in payload["text"]
        assert payload["blocks"][1]["text"]["text"] == "1 finding is ignored or acknowledged."
