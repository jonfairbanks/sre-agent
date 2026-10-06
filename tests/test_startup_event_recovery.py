"""Recovered first starts are history; failed and uncertain starts stay visible."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as k

import scheduler
from tools import k8s_client


START = datetime(2026, 10, 3, 4, 37, 37, tzinfo=timezone.utc)
NOW = START + timedelta(minutes=10)


def startup(ready_seconds=47, event_seconds=37, name="hermes-agent"):
    pod = k.V1Pod(
        metadata=k.V1ObjectMeta(namespace="apps", name=f"{name}-1", uid="current-pod",
                               creation_timestamp=START - timedelta(seconds=53)),
        spec=k.V1PodSpec(containers=[k.V1Container(
            name=name, startup_probe=k.V1Probe(period_seconds=10, failure_threshold=30))]),
        status=k.V1PodStatus(
            phase="Running",
            conditions=[k.V1PodCondition(type="Ready", status="True",
                                        last_transition_time=START + timedelta(seconds=ready_seconds))],
            container_statuses=[k.V1ContainerStatus(
                name=name, image="example:test", image_id="test-id", ready=True,
                started=True, restart_count=0,
                state=k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=START)),
                last_state=k.V1ContainerState())]),
    )
    event = k.CoreV1Event(
        metadata=k.V1ObjectMeta(namespace="apps"),
        involved_object=k.V1ObjectReference(kind="Pod", name=pod.metadata.name,
                                          uid=pod.metadata.uid,
                                          field_path=f"spec.containers{{{name}}}"),
        reason="Unhealthy", type="Warning", count=4,
        message="Startup probe failed: connection refused",
        first_timestamp=START + timedelta(seconds=7),
        last_timestamp=START + timedelta(seconds=event_seconds),
    )
    return pod, event


@pytest.mark.parametrize("name,ready,event_at", [("hermes-agent", 47, 37), ("main", 39, 29)])
def test_observed_rollouts_are_recovered(name, ready, event_at):
    pod, event = startup(ready, event_at, name)
    assert scheduler._is_recovered_startup_event(event, pod, NOW)


@pytest.mark.parametrize("path,value", [
    ("status.phase", "Pending"),
    ("status.container_statuses.0.ready", False),
    ("status.container_statuses.0.started", False),
    ("status.container_statuses.0.started", None),
    ("status.container_statuses.0.restart_count", 1),
    ("status.container_statuses.0.state.running", None),
    ("status.container_statuses.0.state.running.started_at", None),
    ("status.conditions.0.status", "False"),
    ("status.conditions.0.last_transition_time", None),
    ("status.conditions.0.last_transition_time", START + timedelta(seconds=301)),
    ("spec.containers.0.startup_probe", None),
    ("spec.containers.0.startup_probe.period_seconds", 0),
    ("spec.containers.0.startup_probe.failure_threshold", 0),
])
def test_unhealthy_or_incomplete_pod_evidence_is_not_recovered(path, value):
    pod, event = startup()
    target = pod
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else getattr(target, part)
    setattr(target, parts[-1], value)
    assert not scheduler._is_recovered_startup_event(event, pod, NOW)


@pytest.mark.parametrize("path,value", [
    ("reason", "Killing"),
    ("message", "Liveness probe failed: connection refused"),
    ("message", "Readiness probe failed: connection refused"),
    ("involved_object.kind", "Deployment"),
    ("involved_object.uid", "previous-pod"),
    ("involved_object.uid", None),
    ("involved_object.field_path", "spec.containers{other}"),
    ("involved_object.field_path", "spec.initContainers{hermes-agent}"),
    ("last_timestamp", None),
    ("last_timestamp", START - timedelta(seconds=1)),
    ("last_timestamp", START + timedelta(seconds=47)),
    ("last_timestamp", START + timedelta(seconds=48)),
])
def test_other_or_uncertain_events_are_not_recovered(path, value):
    pod, event = startup()
    target = event
    parts = path.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)
    assert not scheduler._is_recovered_startup_event(event, pod, NOW)


def test_latest_series_observation_prevents_false_recovery():
    pod, event = startup()
    event.series = k.CoreV1EventSeries(count=5, last_observed_time=START + timedelta(seconds=48))
    assert not scheduler._is_recovered_startup_event(event, pod, NOW)
    event.series.last_observed_time = START + timedelta(seconds=40)
    assert scheduler._is_recovered_startup_event(event, pod, NOW)


def test_event_time_is_used_when_last_timestamp_is_missing():
    pod, event = startup()
    event.event_time, event.last_timestamp = event.last_timestamp, None
    assert scheduler._is_recovered_startup_event(event, pod, NOW)


def test_default_probe_window_and_initial_delay():
    pod, event = startup(ready_seconds=31, event_seconds=20)
    probe = pod.spec.containers[0].startup_probe
    probe.period_seconds = probe.failure_threshold = None
    assert not scheduler._is_recovered_startup_event(event, pod, NOW)
    probe.initial_delay_seconds = 5
    assert scheduler._is_recovered_startup_event(event, pod, NOW)


def test_missing_container_reference_is_only_safe_for_one_container():
    pod, event = startup()
    event.involved_object.field_path = None
    assert scheduler._is_recovered_startup_event(event, pod, NOW)
    pod.spec.containers.append(k.V1Container(name="sidecar"))
    assert not scheduler._is_recovered_startup_event(event, pod, NOW)


def test_named_container_with_healthy_sidecar_can_recover():
    pod, event = startup()
    pod.spec.containers.append(k.V1Container(name="sidecar"))
    cs = k.V1ContainerStatus(name="sidecar", image="test", image_id="test-id",
                             ready=True, restart_count=0, state=k.V1ContainerState())
    pod.status.container_statuses.append(cs)
    assert scheduler._is_recovered_startup_event(event, pod, NOW)
    cs.ready = False
    assert not scheduler._is_recovered_startup_event(event, pod, NOW)


def mock_cluster(monkeypatch, pods, events):
    core = NS(list_persistent_volume=lambda: NS(items=[]),
              list_node=lambda: NS(items=[]),
              list_pod_for_all_namespaces=lambda: NS(items=pods),
              list_event_for_all_namespaces=lambda **kw: NS(items=events))
    monkeypatch.setattr(k8s_client, "core_v1", lambda: core)
    monkeypatch.setattr(k8s_client, "apps_v1", lambda: NS(
        list_deployment_for_all_namespaces=lambda: NS(items=[])))
    monkeypatch.setattr(k8s_client, "autoscaling_v2", lambda: NS(
        list_horizontal_pod_autoscaler_for_all_namespaces=lambda: NS(items=[])))
    monkeypatch.setattr(k8s_client, "custom_objects", lambda: NS(
        list_cluster_custom_object=lambda *args: {"items": []}))


def test_collector_keeps_evidence_but_model_sees_only_actionable_warning(monkeypatch):
    recovered, old = startup()
    failing, active = startup(name="broken")
    failing.status.container_statuses[0].ready = False
    # Fixtures are retained events relative to the real collector clock.
    shift = datetime.now(timezone.utc) - NOW
    for pod, event in [(recovered, old), (failing, active)]:
        pod.metadata.creation_timestamp += shift
        pod.status.conditions[0].last_transition_time += shift
        pod.status.container_statuses[0].state.running.started_at += shift
        event.first_timestamp += shift
        event.last_timestamp += shift
    mock_cluster(monkeypatch, [recovered, failing], [old, active])
    captured = []
    monkeypatch.setattr(scheduler, "_analyse_snapshot", lambda text: captured.append(text))
    _, data = scheduler.run_structured_health_check()
    assert data["errors"] == []
    assert len(data["recovered_startup_events"]) == 1
    assert data["recovered_startup_events"][0]["count"] == 4
    assert [e["object"] for e in data["events"]] == ["Pod/broken-1"]
    assert "Pod/hermes-agent-1" not in captured[0]
    assert "Pod/broken-1" in captured[0]


def test_collector_keeps_warning_when_timestamp_is_unknown(monkeypatch):
    pod, event = startup()
    event.last_timestamp = None
    mock_cluster(monkeypatch, [pod], [event])
    data = scheduler._collect_cluster_data()
    assert data["events"][0]["age_min"] is None
    assert data["recovered_startup_events"] == []
