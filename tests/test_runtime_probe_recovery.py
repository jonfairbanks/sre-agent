"""Recovered single runtime probe failures retain evidence without alerting."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as k

import scheduler
from tools import k8s_client


NOW = datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc)
WARNING = NOW - timedelta(minutes=33)
START = NOW - timedelta(days=23)


def runtime_probe(kind="Readiness"):
    pod = k.V1Pod(
        metadata=k.V1ObjectMeta(namespace="longhorn-system", name="longhorn-manager-1",
                               uid="current-pod", creation_timestamp=START),
        spec=k.V1PodSpec(node_name="worker-1", containers=[k.V1Container(
            name="longhorn-manager",
            readiness_probe=k.V1Probe(period_seconds=10, failure_threshold=3, timeout_seconds=1),
            liveness_probe=k.V1Probe(period_seconds=10, failure_threshold=3, timeout_seconds=1))]),
        status=k.V1PodStatus(
            phase="Running",
            conditions=[k.V1PodCondition(type="Ready", status="True",
                                        last_transition_time=START + timedelta(seconds=30))],
            container_statuses=[k.V1ContainerStatus(
                name="longhorn-manager", image="example:test", image_id="test-id",
                ready=True, started=True, restart_count=1,
                state=k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=START)),
                last_state=k.V1ContainerState(terminated=k.V1ContainerStateTerminated(
                    reason="Error", exit_code=1, finished_at=START - timedelta(seconds=1))))]),
    )
    event = k.CoreV1Event(
        metadata=k.V1ObjectMeta(namespace=pod.metadata.namespace),
        involved_object=k.V1ObjectReference(kind="Pod", name=pod.metadata.name,
                                          uid=pod.metadata.uid,
                                          field_path="spec.containers{longhorn-manager}"),
        reason="Unhealthy", type="Warning", count=1,
        message=f"{kind} probe failed: context deadline exceeded",
        first_timestamp=WARNING, last_timestamp=WARNING,
    )
    return pod, event


def set_path(target, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else getattr(target, part)
    # These cases intentionally corrupt API data. New client models validate
    # assignments, so bypass validation to keep exercising collector guards.
    object.__setattr__(target, parts[-1], value)


@pytest.mark.parametrize("kind", ["Readiness", "Liveness"])
def test_single_longhorn_probe_after_33_minutes_with_23_day_restart_history(kind):
    pod, event = runtime_probe(kind)
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


@pytest.mark.parametrize("path,value", [
    ("reason", "Killing"),
    ("message", "Startup probe failed: context deadline exceeded"),
    ("message", "readiness probe failed: context deadline exceeded"),
    ("message", "Readiness probe succeeded"),
    ("message", None),
    ("involved_object.kind", "Deployment"),
    ("involved_object.uid", "previous-pod"),
    ("involved_object.uid", None),
    ("involved_object.field_path", "spec.containers{other}"),
    ("involved_object.field_path", "spec.initContainers{longhorn-manager}"),
    ("count", None),
    ("count", 0),
    ("count", 2),
    ("last_timestamp", None),
    ("last_timestamp", NOW + timedelta(seconds=1)),
    ("last_timestamp", "not-a-timestamp"),
    ("last_timestamp", NOW.replace(tzinfo=None)),
])
def test_repeated_other_or_uncertain_warnings_stay_active(path, value):
    pod, event = runtime_probe()
    set_path(event, path, value)
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


@pytest.mark.parametrize("path,value", [
    ("status.phase", "Pending"),
    ("status.container_statuses", []),
    ("status.container_statuses.0.ready", False),
    ("status.container_statuses.0.started", False),
    ("status.container_statuses.0.started", None),
    ("status.container_statuses.0.state.running", None),
    ("status.container_statuses.0.state.running.started_at", None),
    ("status.container_statuses.0.state.running.started_at", WARNING + timedelta(seconds=1)),
    ("status.container_statuses.0.state.running.started_at", "invalid"),
    ("status.container_statuses.0.name", "other"),
    ("status.conditions", []),
    ("status.conditions.0.status", "False"),
    ("spec.containers.0.readiness_probe", None),
    ("spec.containers.0.readiness_probe.period_seconds", 0),
    ("spec.containers.0.readiness_probe.failure_threshold", 0),
    ("spec.containers.0.readiness_probe.timeout_seconds", 0),
])
def test_unhealthy_or_incomplete_current_pod_evidence_stays_active(path, value):
    pod, event = runtime_probe()
    set_path(pod, path, value)
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


def test_recent_failed_termination_stays_active_even_when_current_container_is_ready():
    pod, event = runtime_probe()
    pod.status.container_statuses[0].last_state.terminated.finished_at = NOW - timedelta(minutes=40)
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


@pytest.mark.parametrize("kind,missing_probe", [("Readiness", "readiness_probe"),
                                                ("Liveness", "liveness_probe")])
def test_other_probe_configuration_does_not_prove_recovery(kind, missing_probe):
    pod, event = runtime_probe(kind)
    setattr(pod.spec.containers[0], missing_probe, None)
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


@pytest.mark.parametrize("age_seconds,expected", [(59, False), (60, True), (61, True)])
def test_minimum_recovery_observation_window(age_seconds, expected):
    pod, event = runtime_probe()
    event.first_timestamp = event.last_timestamp = NOW - timedelta(seconds=age_seconds)
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW) is expected


@pytest.mark.parametrize("age_seconds,expected", [(150, False), (151, True), (152, True)])
def test_probe_failure_budget_extends_recovery_window(age_seconds, expected):
    pod, event = runtime_probe()
    probe = pod.spec.containers[0].readiness_probe
    probe.period_seconds, probe.failure_threshold, probe.timeout_seconds = 30, 5, 1
    event.first_timestamp = event.last_timestamp = NOW - timedelta(seconds=age_seconds)
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW) is expected


def test_default_probe_parameters_allow_recovery_after_minimum_window():
    pod, event = runtime_probe()
    probe = pod.spec.containers[0].readiness_probe
    probe.period_seconds = probe.failure_threshold = probe.timeout_seconds = None
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


def test_timeout_longer_than_period_extends_recovery_window():
    pod, event = runtime_probe()
    probe = pod.spec.containers[0].readiness_probe
    probe.period_seconds, probe.failure_threshold, probe.timeout_seconds = 10, 3, 30
    event.first_timestamp = event.last_timestamp = NOW - timedelta(seconds=100)
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


@pytest.mark.parametrize("count,series_count,expected", [(1, 1, True), (1, 2, False),
                                                       (2, 1, False), (None, 1, True)])
def test_series_and_legacy_counts_use_latest_known_occurrence_count(count, series_count, expected):
    pod, event = runtime_probe()
    event.count = count
    event.series = k.CoreV1EventSeries(count=series_count, last_observed_time=WARNING)
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW) is expected


def test_unknown_series_count_does_not_invent_a_single_occurrence():
    pod, event = runtime_probe()
    event.count = None
    object.__setattr__(event, "series", NS(count=None, last_observed_time=WARNING))
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


def test_latest_series_observation_keeps_a_recent_warning_active():
    pod, event = runtime_probe()
    event.series = k.CoreV1EventSeries(count=1, last_observed_time=NOW - timedelta(seconds=30))
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


def test_event_time_can_prove_recovery_when_legacy_timestamp_is_missing():
    pod, event = runtime_probe()
    event.event_time, event.last_timestamp = event.last_timestamp, None
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


def test_missing_container_reference_is_only_unambiguous_for_one_container():
    pod, event = runtime_probe()
    event.involved_object.field_path = None
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW)
    pod.spec.containers.append(k.V1Container(name="sidecar"))
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


@pytest.mark.parametrize("attribute,value", [("ready", False), ("started", False),
                                            ("started", None), ("state", k.V1ContainerState())])
def test_unhealthy_or_unproven_sidecar_keeps_warning_active(attribute, value):
    pod, event = runtime_probe()
    pod.spec.containers.append(k.V1Container(name="sidecar"))
    sidecar = k.V1ContainerStatus(
        name="sidecar", image="test", image_id="test-id", ready=True, started=True,
        restart_count=0, state=k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=START)))
    pod.status.container_statuses.append(sidecar)
    assert scheduler._is_recovered_runtime_probe_event(event, pod, NOW)
    setattr(sidecar, attribute, value)
    assert not scheduler._is_recovered_runtime_probe_event(event, pod, NOW)


def mock_cluster(monkeypatch, pods, events, node_ready=True):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(scheduler, "datetime", FixedDatetime)
    node = k.V1Node(metadata=k.V1ObjectMeta(name="worker-1"), status=k.V1NodeStatus(
        conditions=[k.V1NodeCondition(type="Ready", status="True" if node_ready else "False")]))
    core = NS(list_persistent_volume=lambda: NS(items=[]),
              list_node=lambda: NS(items=[node]),
              list_pod_for_all_namespaces=lambda: NS(items=pods),
              list_event_for_all_namespaces=lambda **kw: NS(items=events),
              connect_get_node_proxy_with_path=lambda *args, **kw: NS(data='{"node": {"fs": {"capacityBytes": 1000000, "usedBytes": 100000}}, "pods": []}'))
    monkeypatch.setattr(k8s_client, "core_v1", lambda: core)
    monkeypatch.setattr(k8s_client, "apps_v1", lambda: NS(
        list_deployment_for_all_namespaces=lambda: NS(items=[])))
    monkeypatch.setattr(k8s_client, "autoscaling_v2", lambda: NS(
        list_horizontal_pod_autoscaler_for_all_namespaces=lambda: NS(items=[])))
    monkeypatch.setattr(k8s_client, "custom_objects", lambda: NS(
        list_cluster_custom_object=lambda *args: {"items": []}))


def test_collector_retains_paired_longhorn_evidence_but_model_sees_active_warning_only(monkeypatch):
    pod, readiness = runtime_probe("Readiness")
    _, liveness = runtime_probe("Liveness")
    failing, repeated = runtime_probe("Readiness")
    failing.metadata.name = repeated.involved_object.name = "longhorn-manager-broken"
    failing.metadata.uid = repeated.involved_object.uid = "broken-pod"
    repeated.count = 2
    mock_cluster(monkeypatch, [pod, failing], [readiness, liveness, repeated])
    data = scheduler._collect_cluster_data()
    assert data["errors"] == []
    assert len(data["recovered_runtime_probe_events"]) == 2
    assert all(e["age_min"] == 33 and e["count"] == 1 for e in data["recovered_runtime_probe_events"])
    assert [e["count"] for e in data["events"]] == [2]
    snapshot = scheduler._format_snapshot(data)
    assert snapshot.count("Readiness probe failed:") == 1
    assert "Liveness probe failed:" not in snapshot


def test_separate_messages_for_same_probe_are_repeated_evidence(monkeypatch):
    pod, event = runtime_probe()
    _, different = runtime_probe()
    different.message = "Readiness probe failed: connection refused"
    mock_cluster(monkeypatch, [pod], [event, different])
    data = scheduler._collect_cluster_data()
    assert data["errors"] == []
    assert len(data["events"]) == 2
    assert data["recovered_runtime_probe_events"] == []


def test_repeated_probe_evidence_beyond_event_cap_prevents_recovery(monkeypatch):
    pod, event = runtime_probe()
    _, older = runtime_probe()
    older.message = "Readiness probe failed: connection refused"
    older.first_timestamp = older.last_timestamp = NOW - timedelta(minutes=40)
    fillers = [k.CoreV1Event(
        metadata=k.V1ObjectMeta(namespace="longhorn-system"),
        involved_object=k.V1ObjectReference(kind="Node", name=f"other-{i}"),
        reason="Failed", message="unrelated warning", count=1,
        last_timestamp=NOW - timedelta(minutes=35)) for i in range(20)]
    mock_cluster(monkeypatch, [pod], [event, *fillers, older])
    data = scheduler._collect_cluster_data()
    assert data["errors"] == []
    assert any(e["message"] == event.message for e in data["events"])
    assert data["recovered_runtime_probe_events"] == []


def test_node_not_ready_keeps_probe_warning_active(monkeypatch):
    pod, event = runtime_probe()
    mock_cluster(monkeypatch, [pod], [event], node_ready=False)
    data = scheduler._collect_cluster_data()
    assert data["errors"] == []
    assert len(data["events"]) == 1
    assert data["recovered_runtime_probe_events"] == []
