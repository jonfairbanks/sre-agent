"""Incident state follows observed facts, not model wording or missing data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import sleep
from types import SimpleNamespace

import scheduler
from health_evidence import reconcile_report
from monitor_state import StoredFinding, deserialize_diff, diff_report, fingerprint, serialize_diff
from schemas import Finding, HealthReport


def snapshot(**updates):
    data = {"nodes": [], "pods": [], "unhealthy_pods": [], "events": [], "hpas": [],
            "deployments": [], "node_metrics": [], "pod_metrics": {}, "node_disk_usage": {},
            "pvc_usage": {}, "local_filesystem_usage": {}, "errors": []}
    data.update(updates)
    return data


def report(findings=(), **updates):
    return HealthReport(overall_severity="ok", summary="Cluster healthy.", findings=list(findings), **updates)


def stored_for(finding, **updates):
    return StoredFinding(fingerprint=fingerprint(finding), severity=finding.severity, title=finding.title,
                         namespace=finding.namespace, first_seen=datetime.now(timezone.utc), times_seen=2,
                         **updates)


def test_disk_identity_and_severity_ignore_model_rewording():
    data = snapshot(node_disk_usage={"worker": {"percent": 82, "local_claims": ["app/data"]}})
    model = Finding(kind="Node", resource_name="worker", severity="critical", title="Disk Nearly Full",
                    detail="Disk usage 82%", reason="DiskAlmostFull")
    first = reconcile_report(report([model]), data)
    second = reconcile_report(report([model.model_copy(update={"title": "Storage Usage", "reason": "DiskPressure", "severity": "info"})]), data)
    assert [f.model_dump() for f in first.findings] == [f.model_dump() for f in second.findings]
    finding = first.findings[0]
    assert finding.reason == "NodeFilesystemUsage"
    assert finding.severity == "warning"
    previous = stored_for(finding)
    diff = diff_report(second, {previous.fingerprint: previous})
    assert len(diff.ongoing) == 1
    assert not diff.new and not diff.resolved and not diff.escalated


def test_shared_node_disk_cannot_be_recast_as_pvc_usage():
    data = snapshot(node_disk_usage={"worker": {"percent": 82, "local_claims": ["app/data"]}})
    model = Finding(kind="PersistentVolumeClaim", namespace="app", resource_name="data", severity="critical",
                    title="PVC Disk Full", detail="Expand the volume", reason="PVCUsage")
    result = reconcile_report(report([model]), data)
    assert [(f.kind, f.reason) for f in result.findings] == [("Node", "NodeFilesystemUsage")]


def test_disk_hysteresis_holds_until_clear_threshold():
    initial = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 72}}))
    previous = stored_for(initial.findings[0])
    stored = {previous.fingerprint: previous}
    assert not reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 68}})).findings
    held = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 68}}), stored)
    assert fingerprint(held.findings[0]) == previous.fingerprint
    assert not diff_report(held, stored).resolved
    cleared = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 64}}), stored)
    assert len(diff_report(cleared, stored).resolved) == 1


def test_failed_analysis_and_partial_collection_do_not_resolve_incidents():
    finding = Finding(kind="Pod", namespace="app", resource_name="worker", severity="critical",
                      title="Failure", detail="Observed fault", reason="CrashLoopBackOff")
    previous = stored_for(finding)
    stored = {previous.fingerprint: previous}
    for failed in (reconcile_report(report(analysis_valid=False), snapshot()),
                   reconcile_report(report(), snapshot(errors=["pods: API unavailable"]))):
        diff = diff_report(failed, stored)
        assert failed.overall_severity == "unknown"
        assert diff.retained == [previous]
        assert not diff.resolved
        assert "unknown" in failed.summary


def test_true_oom_and_activation_faults_are_immediate_even_if_analysis_fails():
    data = snapshot(unhealthy_pods=[{"name": "worker-0", "namespace": "app", "status": "Restarted/OOMKilled",
                                    "last_termination": "OOMKilled"}],
                    hpas=[{"name": "worker", "namespace": "app", "current": 0, "max": 1,
                           "conditions": [], "keda": {"idle": False, "health": {},
                           "conditions": [{"type": "Ready", "status": "False"}]}}])
    result = reconcile_report(report(analysis_valid=False), data)
    assert {f.reason for f in result.findings} == {"OOMKilled", "KEDAActivationFailure"}
    assert len(diff_report(result, {}).new) == 2


def test_verified_idle_and_fixed_hpa_do_not_create_findings():
    data = snapshot(hpas=[{"name": "worker", "namespace": "app", "min": 1, "max": 1, "current": 0,
                           "conditions": [{"type": "ScalingActive", "status": "False", "reason": "ScalingDisabled"}],
                           "keda": {"idle": True, "conditions": [], "health": {}}}])
    assert not reconcile_report(report(), data).findings


def test_independent_model_observations_remain_available():
    observation = Finding(kind="Deployment", namespace="app", resource_name="worker", severity="info",
                          title="Missing Limits", detail="Consider resource limits", reason="MissingResourceLimits")
    assert reconcile_report(report([observation]), snapshot()).findings == [observation]


def test_diff_round_trip_includes_retained_and_ignore_state():
    finding = Finding(severity="warning", title="Disk Usage", detail="Observed", kind="Node", resource_name="worker",
                      reason="NodeFilesystemUsage")
    previous = stored_for(finding, ignored_until=datetime.now(timezone.utc) + timedelta(hours=1))
    diff = diff_report(report(analysis_valid=False), {previous.fingerprint: previous})
    assert deserialize_diff(serialize_diff(diff)) == diff


def test_explicit_ignore_suppresses_escalation_and_expiry_realerts():
    finding = Finding(severity="critical", title="Fault", detail="Observed", kind="Pod", resource_name="worker", reason="OOMKilled")
    previous = stored_for(finding, ignored_forever=True)
    previous.severity = "warning"
    stored = {previous.fingerprint: previous}
    assert len(diff_report(report([finding]), stored).suppressed) == 1
    previous.ignored_forever = False
    previous.mute_expired = True
    diff = diff_report(report([finding]), stored)
    assert len(diff.new) == 1
    assert diff.new[0].first_seen == previous.first_seen


def test_concurrent_manual_and_scheduled_checks_are_serialized(monkeypatch):
    guard = Lock()
    active = 0
    peak = 0
    calls = []

    def collect():
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        sleep(.03)
        with guard:
            active -= 1
        return report(), snapshot()

    monkeypatch.setattr(scheduler, "run_structured_health_check", collect)
    db = SimpleNamespace(available=True, next_check_number=lambda: len(calls) + 1,
                         load_tracked_findings=lambda: {},
                         record_monitor_check=lambda *args: calls.append(args))
    monitor = scheduler.MonitoringScheduler(None, SimpleNamespace(enabled=False), db=db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(monitor._do_check, ["manual", "scheduled"]))
    assert peak == 1
    assert [args[1] for args in calls] == [1, 2]


def test_missing_filesystem_sample_preserves_disk_incident():
    initial = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 82}}))
    previous = stored_for(initial.findings[0])
    stored = {previous.fingerprint: previous}
    missing = reconcile_report(report(), snapshot(), stored)
    assert missing.overall_severity == "unknown"
    assert diff_report(missing, stored).retained == [previous]


def test_critical_disk_threshold_has_its_own_clear_boundary():
    initial = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 92}}))
    previous = stored_for(initial.findings[0])
    stored = {previous.fingerprint: previous}
    held = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 89}}), stored)
    assert held.findings[0].severity == "critical"
    lowered = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 84}}), stored)
    assert lowered.findings[0].severity == "warning"


def test_unknown_node_readiness_is_not_reported_healthy_or_broken():
    unknown = reconcile_report(report(), snapshot(nodes=[{"name": "worker", "status": "Unknown"}]))
    assert unknown.overall_severity == "unknown"
    assert not unknown.findings


def test_unrelated_collection_failure_does_not_block_proven_disk_recovery():
    initial = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 82}}))
    previous = stored_for(initial.findings[0])
    stored = {previous.fingerprint: previous}
    current = reconcile_report(report(), snapshot(node_disk_usage={"worker": {"percent": 62}},
                                                   errors=["pod metrics: unavailable"]), stored)
    assert len(diff_report(current, stored).resolved) == 1


def test_source_failure_blocks_resolution_of_corresponding_incident():
    finding = Finding(kind="Pod", namespace="app", resource_name="worker", severity="critical",
                      title="CrashLoopBackOff", detail="Observed", reason="CrashLoopBackOff")
    previous = stored_for(finding)
    stored = {previous.fingerprint: previous}
    current = reconcile_report(report(), snapshot(errors=["pods: unavailable"]), stored)
    assert diff_report(current, stored).retained == [previous]


def test_report_ack_keeps_existing_escalation_suppression():
    finding = Finding(kind="Pod", namespace="app", resource_name="worker", severity="critical",
                      title="OOMKilled", detail="Observed", reason="OOMKilled")
    previous = stored_for(finding, ack_until=datetime.now(timezone.utc) + timedelta(hours=1))
    previous.severity = "warning"
    diff = diff_report(report([finding]), {previous.fingerprint: previous})
    assert len(diff.suppressed) == 1
    assert diff.suppressed[0].status == "escalated"
    assert not diff.escalated


def test_monitor_status_changes_notify_once_and_recovery_notifies(monkeypatch):
    persisted = []
    queued = []
    current = [report(analysis_valid=False), snapshot()]
    monkeypatch.setattr(scheduler, "run_structured_health_check", lambda: tuple(current))
    monkeypatch.setattr(scheduler, "MONITOR_DIGEST_EVERY_N_CHECKS", 0)

    def record(session_id, check_no, report, data, diff, now, notification):
        persisted.append({"report": report.model_dump(mode="json")})
        if notification:
            queued.append(notification)

    db = SimpleNamespace(available=True, next_check_number=lambda: len(persisted) + 1,
                         load_tracked_findings=lambda: {}, recent_monitor_checks=lambda limit: persisted[-limit:],
                         record_monitor_check=record)
    monitor = scheduler.MonitoringScheduler(None, SimpleNamespace(enabled=True), db=db)
    monitor._do_check("failure")
    monitor._do_check("same-failure")
    current[0] = report()
    monitor._do_check("recovery")
    monitor._do_check("still-healthy")
    assert len(persisted) == 4
    assert len(queued) == 2
    assert [item["report"]["analysis_valid"] for item in queued] == [False, True]
    assert all(item["source"] == "scheduled status" for item in queued)


def test_collection_errors_do_not_expose_api_bodies_or_credentials():
    from kubernetes.client.rest import ApiException
    error = ApiException(status=403, reason="sensitive-token")
    error.body = "private response with sensitive-token"
    assert scheduler._collection_failure(error) == "ApiException (HTTP 403 Forbidden)"


def test_ignored_recovery_is_recorded_without_notification():
    finding = Finding(kind="Pod", namespace="app", resource_name="worker", severity="critical",
                      title="OOMKilled", detail="Observed", reason="OOMKilled")
    previous = stored_for(finding, ignored_forever=True)
    stored = {previous.fingerprint: previous}
    result = diff_report(reconcile_report(report(), snapshot()), stored)
    assert len(result.resolved) == 1
    assert result.resolved[0].suppressed is True
    assert not result.should_notify()
    assert "resolved" not in result.summary_line()
    assert deserialize_diff(serialize_diff(result)) == result


def test_ignored_recurring_incident_remains_suppressed_after_resolution():
    finding = Finding(kind="Pod", namespace="app", resource_name="worker", severity="critical",
                      title="OOMKilled", detail="Observed", reason="OOMKilled")
    previous = stored_for(finding, ignored_forever=True, resolved_at=datetime.now(timezone.utc))
    result = diff_report(report([finding]), {previous.fingerprint: previous})
    assert len(result.suppressed) == 1
    assert result.suppressed[0].status == "new"
    assert not result.should_notify()


def test_intentional_hpa_cap_without_workload_harm_is_healthy():
    hpa = {"name": "worker", "namespace": "app", "min": 1, "max": 50, "current": 50,
           "conditions": [{"type": "ScalingLimited", "status": "True", "reason": "TooManyReplicas"}]}
    model = Finding(kind="HPA", namespace="app", resource_name="worker", severity="warning",
                    title="Replica Capacity Limit", detail="Autoscaler is at its maximum replica count",
                    reason="CapacityLimit")
    result = reconcile_report(report([model]), snapshot(hpas=[hpa]))
    assert result.overall_severity == "ok"
    assert not result.findings


def test_explicit_metric_failure_at_intentional_hpa_cap_still_alerts():
    hpa = {"name": "worker", "namespace": "app", "min": 1, "max": 50, "current": 50,
           "conditions": [{"type": "ScalingLimited", "status": "True", "reason": "TooManyReplicas"},
                          {"type": "ScalingActive", "status": "False", "reason": "FailedGetResourceMetric"}]}
    result = reconcile_report(report(), snapshot(hpas=[hpa]))
    assert result.overall_severity == "warning"
    assert [f.reason for f in result.findings] == ["FailedGetResourceMetric"]


def test_model_prompt_preserves_intentional_replica_cap():
    prompt, _ = scheduler._health_prompt("snapshot")
    assert "Do not flag replica equality or TooManyReplicas alone" in prompt
    assert "without independent evidence of workload harm" in prompt
