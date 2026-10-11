"""Keep termination clock evidence separate from an Unknown reason token."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from health_evidence import _pod_evidence_detail, reconcile_report
from monitor_state import fingerprint
from scheduler import _classify_pod
from schemas import Finding, HealthReport

@pytest.fixture(autouse=True)
def display_timezone(monkeypatch):
    monkeypatch.setenv("ALERT_TIMEZONE", "America/Los_Angeles")


NOW = datetime(2026, 10, 11, 3, 7, 15, tzinfo=timezone.utc)
FINISHED = datetime(2026, 10, 11, 2, 37, 15, tzinfo=timezone.utc)


def container(name="app", reason="Unknown", exit_code=255, finished_at=FINISHED, signal=9):
    term = NS(reason=reason, exit_code=exit_code, finished_at=finished_at, signal=signal)
    return NS(name=name, ready=True, restart_count=1,
              last_state=NS(terminated=term), state=NS(terminated=None, waiting=None))


def pod(containers=None, phase="Running"):
    return NS(metadata=NS(creation_timestamp=NOW - timedelta(days=2)),
              status=NS(phase=phase, container_statuses=containers or [container()]))


def row(extra, status):
    return {"name": "api-7d9f8b6c4-abcde", "namespace": "app", "status": status, **extra}


def finding_for(data):
    report = HealthReport(overall_severity="ok", summary="Checked.")
    return reconcile_report(report, {"unhealthy_pods": [data]}).findings[0]


def test_unknown_reason_keeps_known_source_time_and_pacific_clock_without_exit_alert():
    unhealthy, status, extra = _classify_pod(pod(), NOW)
    assert unhealthy is False and status == "Running"
    assert extra["last_termination_finished_at"] == "2026-10-11T02:37:15+00:00"
    assert extra["last_termination_container"] == "app"
    assert extra["last_exit_code"] == 255
    assert extra["last_signal"] == 9
    finding = finding_for(row(extra, "Restarted/Unknown"))
    assert finding.title == "Recent Container Restart on api-7d9f8b6c4-abcde"
    assert "Container app last terminated Oct 10, 2026 at 7:37:15 PM PDT (30m ago)" in finding.detail
    assert "2026-10-11T02:37:15+00:00" not in finding.detail
    assert "Kubernetes reason: Unknown (no specific reason supplied)" in finding.detail
    assert "exit" not in finding.detail.lower()
    assert "signal" not in finding.detail.lower()
    previous = Finding(severity="critical", title="Unknown on api", detail="Old wording", kind="Pod",
                       resource_name="api-7d9f8b6c4-abcde", namespace="app", reason="Unknown")
    assert fingerprint(finding) == fingerprint(previous)


def test_oom_keeps_reason_timestamp_and_internal_exit_evidence():
    unhealthy, status, extra = _classify_pod(pod([container(reason="OOMKilled", exit_code=137)]), NOW)
    assert unhealthy is False and status == "Running"
    assert extra["last_exit_code"] == 137
    finding = finding_for(row(extra, "Restarted/OOMKilled"))
    assert finding.reason == "OOMKilled" and finding.severity == "critical"
    assert "7:37:15 PM PDT" in finding.detail
    assert "Kubernetes reason: OOMKilled" in finding.detail
    assert "137" not in finding.detail


def test_latest_container_is_selected_with_its_own_metadata():
    old = container(name="old", reason="Error", exit_code=1, finished_at=FINISHED - timedelta(minutes=10))
    newest = container(name="new", reason="Unknown", exit_code=255)
    unhealthy, status, extra = _classify_pod(pod([old, newest]), NOW)
    assert unhealthy is False and status == "Running"
    assert extra["last_termination_container"] == "new"
    assert extra["last_exit_code"] == 255
    assert len(extra["last_termination_records"]) == 1
    assert "Container new" in finding_for(row(extra, status)).detail


def test_latest_time_ties_preserve_all_containers_in_stable_order():
    first = container(name="b")
    second = container(name="a")
    left = _classify_pod(pod([first, second]), NOW)[2]
    right = _classify_pod(pod([second, first]), NOW)[2]
    assert left == right
    assert left["last_termination_container"] == "a"
    detail = finding_for(row(left, "Restarted/Unknown")).detail
    assert detail.index("Container a") < detail.index("Container b")


def test_missing_finished_at_keeps_failed_pod_policy_and_says_time_unavailable():
    unhealthy, status, extra = _classify_pod(pod([container(finished_at=None)], phase="Failed"), NOW)
    assert unhealthy and status == "Failed"
    assert extra["last_termination_finished_at"] is None
    assert extra["last_termination_min_ago"] is None
    assert extra["last_exit_code"] == 255
    detail = finding_for(row(extra, status)).detail
    assert "Container app: termination time unavailable" in detail
    assert "Kubernetes reason: Unknown" in detail
    assert "finishedAt:" not in detail and " ago" not in detail


def test_missing_finished_at_does_not_make_running_restart_recent():
    unhealthy, status, extra = _classify_pod(pod([container(finished_at=None)]), NOW)
    assert unhealthy is False and status == "Running"
    assert extra["last_termination_finished_at"] is None


@pytest.mark.parametrize("stamp", [None, "Unknown", "x" * 100_000, "2026-10-11T02:37:15", "2026-99-99T99:99:99Z"])
def test_unavailable_or_invalid_clock_is_never_invented(stamp):
    detail = _pod_evidence_detail({"status": "Restarted/Unknown", "last_termination": "Unknown",
                                  "last_termination_container": "app", "last_termination_finished_at": stamp,
                                  "last_termination_min_ago": 30})
    assert "termination time unavailable" in detail
    assert "30m ago" not in detail
    assert len(detail) < 200


def test_clock_uses_pacific_standard_time_in_winter():
    detail = _pod_evidence_detail({"status": "Restarted/Unknown", "last_termination": "Unknown",
                                  "last_termination_container": "app", "last_termination_finished_at": "2026-12-11T02:37:15Z"})
    assert "Dec 10, 2026 at 6:37:15 PM PST" in detail


def test_extreme_calendar_value_and_many_tied_containers_remain_bounded():
    records = [{"container": f"app-{index}", "reason": "Unknown", "finished_at": "0001-01-01T00:00:00Z",
                "minutes_ago": 10**30} for index in range(20)]
    detail = _pod_evidence_detail({"status": "Restarted/Unknown", "last_termination_records": records})
    assert "16 other containers" in detail
    assert "0001-01-01T00:00:00+00:00" in detail
    assert len(detail) < 1500


@pytest.mark.parametrize("stamp", [None, "not-a-clock", FINISHED.replace(tzinfo=None)])
def test_invalid_finished_at_is_unavailable_without_collection_exception(stamp):
    unhealthy, status, extra = _classify_pod(pod([container(finished_at=stamp)], phase="Failed"), NOW)
    assert unhealthy and status == "Failed"
    assert extra["last_termination_finished_at"] is None
    assert extra["last_termination_min_ago"] is None


@pytest.mark.parametrize("stamp", [None, FINISHED - timedelta(days=10)])
def test_current_oom_is_active_even_with_stale_or_missing_clock(stamp):
    current = container(name="active", reason="Completed", exit_code=0)
    current.state.terminated = NS(reason="OOMKilled", exit_code=137, finished_at=stamp, signal=9)
    sidecar = container(name="sidecar", reason="Completed", exit_code=0)
    unhealthy, status, extra = _classify_pod(pod([current, sidecar]), NOW)
    assert unhealthy and status == "OOMKilled"
    detail = finding_for(row(extra, status)).detail
    assert "Current pod state: OOMKilled" in detail
    assert "Container active" in detail
    assert "reason: OOMKilled" in detail
    assert "sidecar" not in detail and "Completed" not in detail
    assert "137" not in detail


@pytest.mark.parametrize("reason,exit_code", [("Unknown", 255), ("Error", 1), ("OOMKilled", 137)])
def test_ready_recovery_is_healthy_but_unready_recent_failure_stays_active(reason, exit_code):
    status = container(reason=reason, exit_code=exit_code)
    assert _classify_pod(pod([status]), NOW)[0] is False
    status.ready = False
    young = pod([status])
    young.metadata.creation_timestamp = NOW - timedelta(minutes=1)
    unhealthy, label, _ = _classify_pod(young, NOW)
    assert unhealthy and label == f"Restarted/{reason}"


@pytest.mark.parametrize("reason,exit_code", [("Unknown", 255), ("Error", 1)])
def test_collector_keeps_recovered_evidence_without_model_critical_or_stale_actions(monkeypatch, reason, exit_code):
    import scheduler
    from tools import k8s_client

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    p = pod([container(reason=reason, exit_code=exit_code)])
    p.metadata.namespace, p.metadata.name, p.metadata.owner_references = "infra", "manager-worker", [
        NS(controller=True, kind="DaemonSet", name="manager")]
    empty = lambda **kwargs: NS(items=[])
    monkeypatch.setattr(scheduler, "datetime", FixedDatetime)
    monkeypatch.setattr(k8s_client, "core_v1", lambda: NS(list_node=empty, list_event_for_all_namespaces=empty,
                                                       list_persistent_volume=empty,
                                                       list_pod_for_all_namespaces=lambda: NS(items=[p])))
    monkeypatch.setattr(k8s_client, "apps_v1", lambda: NS(list_deployment_for_all_namespaces=empty))
    monkeypatch.setattr(k8s_client, "autoscaling_v2", lambda: NS(list_horizontal_pod_autoscaler_for_all_namespaces=empty))
    monkeypatch.setattr(k8s_client, "custom_objects", lambda: NS(list_cluster_custom_object=lambda *args: {"items": []}))
    model = Finding(severity="critical", title="Recent Container Restart", detail="Still failing", kind="Pod",
                    resource_name=p.metadata.name, namespace=p.metadata.namespace, reason=reason)
    monkeypatch.setattr(scheduler, "_analyse_snapshot", lambda text: HealthReport(
        overall_severity="critical", summary="Manager is still failing after a restart.", findings=[model],
        recommended_actions=["Restart the manager workload."]))
    result, data = scheduler.run_structured_health_check()
    assert data["unhealthy_pods"] == []
    assert data["pods"][0]["last_termination_finished_at"] == "2026-10-11T02:37:15+00:00"
    assert data["pods"][0]["last_exit_code"] == exit_code
    assert not result.findings and result.overall_severity == "ok"
    assert result.summary == "No active issues found."
    assert result.recommended_actions == []
    snapshot = scheduler._format_snapshot(data)
    assert "Recovered Container Terminations (History)" in snapshot
    assert "2026-10-11T02:37:15+00:00" in snapshot
    assert "UNHEALTHY PODS" not in snapshot


def test_unknown_owner_incident_requires_verified_pods_for_recovery():
    from monitor_state import StoredFinding, diff_report
    from schemas import CollectionCoverage
    previous = StoredFinding(fingerprint="infra/daemonset/manager:unknown", severity="critical",
                             kind="Pod", reason="Unknown")
    stored = {previous.fingerprint: previous}
    healthy = HealthReport(overall_severity="ok", summary="Healthy.",
                           coverage=[CollectionCoverage(area="pods", status="complete"),
                                     CollectionCoverage(area="node_metrics", status="unavailable")])
    assert len(diff_report(healthy, stored).resolved) == 1
    for report in (healthy.model_copy(update={"analysis_valid": False}),
                   healthy.model_copy(update={"coverage": [CollectionCoverage(area="pods", status="unavailable")]}),
                   healthy.model_copy(update={"coverage": []})):
        diff = diff_report(report, stored)
        assert not diff.resolved and diff.retained == [previous]


def test_recovered_restart_filter_keeps_independent_disk_finding_and_drops_stale_prose():
    historical = Finding(severity="critical", title="Manager Restarted", detail="Fault", kind="DaemonSet",
                         resource_name="manager", namespace="infra", reason="Unknown")
    disk = Finding(severity="warning", title="Alloy Queue Growth", detail="Queue is growing", kind="Deployment",
                   resource_name="alloy", namespace="telemetry", reason="QueueGrowth")
    data = {"pods": [{"name": "manager-worker", "namespace": "infra", "status": "Running", "ready": True,
                      "owner_kind": "DaemonSet", "owner_name": "manager"}], "unhealthy_pods": [],
            "nodes": [], "events": [], "hpas": [], "deployments": [], "node_metrics": [], "pod_metrics": {},
            "node_disk_usage": {"worker": {"percent": 82}}, "pvc_usage": {}, "local_filesystem_usage": {}, "errors": []}
    report = HealthReport(overall_severity="critical", summary="Manager is failing.", findings=[historical, disk],
                          recommended_actions=["Restart manager."])
    result = reconcile_report(report, data)
    assert {f.reason for f in result.findings} == {"NodeFilesystemUsage", "QueueGrowth"}
    assert result.overall_severity == "warning"
    assert "Manager is failing" not in result.summary
    assert result.recommended_actions == []
