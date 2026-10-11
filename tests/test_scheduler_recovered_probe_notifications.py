"""Recovered evidence follows normal notification and digest eligibility."""
from types import SimpleNamespace

import pytest

import scheduler
from schemas import HealthReport


@pytest.mark.parametrize("check,active,expected_posts", [(1, False, 0),
                                                       (12, False, 1),
                                                       (1, True, 1)])
def test_recovered_probe_evidence_does_not_force_a_notification(
    monkeypatch, check, active, expected_posts,
):
    recovered = [{"namespace": "storage", "object": "Pod/engine-image",
                  "message": "Readiness probe failed: command timed out",
                  "count": 1, "age_min": 33}]
    report = HealthReport(
        overall_severity="warning" if active else "ok",
        summary="An active failure." if active else "Cluster healthy.",
        findings=[{"severity": "warning", "title": "Repeated Probe Failures",
                   "detail": "Another pod is unready.", "namespace": "app",
                   "kind": "Pod", "resource_name": "api", "reason": "ProbeFailure"}]
        if active else [],
    )
    monkeypatch.setattr(scheduler, "run_structured_health_check", lambda: (
        report, {"unhealthy_pods": [], "recovered_runtime_probe_events": recovered},
    ))
    monkeypatch.setattr(scheduler, "MONITOR_DIGEST_EVERY_N_CHECKS", 12)
    monkeypatch.setattr(scheduler, "MONITOR_NOTIFY_ON_RESOLVED", True)
    applied, posts, alerts = [], [], []

    def record(session_id, check_no, report, data, diff, now, notification):
        applied.append(diff)
        if notification:
            posts.append(notification)
    database = SimpleNamespace(
        available=True, next_check_number=lambda: check,
        load_tracked_findings=lambda: {},
        record_monitor_check=record,
    )
    notifier = SimpleNamespace(
        enabled=True,
        send_structured_report=lambda report, **kwargs: posts.append((report, kwargs)),
        send_alert=lambda *args: alerts.append(args),
    )

    scheduler.MonitoringScheduler(None, notifier, db=database)._do_check("sched-test")

    assert alerts == []
    assert len(applied) == 1
    assert len(posts) == expected_posts
    if posts:
        assert posts[0]["recovered_probe_events"] == recovered
        from monitor_state import deserialize_diff
        assert bool(deserialize_diff(posts[0]["diff"]).active) is active
        assert posts[0]["source"] == ("scheduled" if active else "scheduled digest")
