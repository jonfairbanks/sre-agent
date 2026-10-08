"""Require correlated KEDA evidence before classifying a zero target as idle."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from kubernetes import client

from scheduler import _collect_cluster_data, _format_snapshot, _keda_hpa_context


@pytest.fixture
def idle_resources():
    hpa = client.V2HorizontalPodAutoscaler(
        metadata=client.V1ObjectMeta(
            namespace="example", name="request-hpa", generation=2,
            owner_references=[client.V1OwnerReference(
                api_version="keda.sh/v1alpha1", kind="ScaledObject",
                name="request-scaler", uid="scaler-uid", controller=True,
            )],
        ),
        spec=client.V2HorizontalPodAutoscalerSpec(
            min_replicas=1, max_replicas=1,
            scale_target_ref=client.V2CrossVersionObjectReference(
                api_version="apps/v1", kind="Deployment", name="worker",
            ),
        ),
        status=client.V2HorizontalPodAutoscalerStatus(
            current_replicas=0, desired_replicas=0, observed_generation=2,
            conditions=[
                client.V2HorizontalPodAutoscalerCondition(
                    type="AbleToScale", status="True", reason="SucceededGetScale",
                ),
                client.V2HorizontalPodAutoscalerCondition(
                    type="ScalingActive", status="False", reason="ScalingDisabled",
                ),
            ],
        ),
    )
    scaled_object = {
        "apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
        "metadata": {"namespace": "example", "name": "request-scaler", "uid": "scaler-uid"},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "worker"},
            "minReplicaCount": 0, "maxReplicaCount": 1,
        },
        "status": {
            "hpaName": "request-hpa",
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "ScaledObjectReady"},
                {"type": "Active", "status": "False", "reason": "ScalerNotActive"},
                {"type": "Paused", "status": "False"},
                {"type": "Fallback", "status": "False"},
            ],
        },
    }
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(namespace="example", name="worker", generation=3),
        spec=client.V1DeploymentSpec(
            replicas=0,
            selector=client.V1LabelSelector(match_labels={"app": "worker"}),
            template=client.V1PodTemplateSpec(),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=3, replicas=0, ready_replicas=0, available_replicas=0,
        ),
    )
    return hpa, scaled_object, deployment


def test_fixed_hpa_at_zero_has_proven_keda_idle_context(idle_resources):
    assert _keda_hpa_context(*idle_resources)["idle"] is True


def test_default_scaledobject_minimum_allows_zero(idle_resources):
    del idle_resources[1]["spec"]["minReplicaCount"]
    assert _keda_hpa_context(*idle_resources)["idle"] is True


def test_explicit_zero_idle_replica_count_is_supported(idle_resources):
    idle_resources[1]["spec"]["idleReplicaCount"] = 0
    assert _keda_hpa_context(*idle_resources)["idle"] is True


def test_nonfixed_hpa_can_also_be_idle(idle_resources):
    idle_resources[0].spec.max_replicas = 5
    assert _keda_hpa_context(*idle_resources)["idle"] is True


def _set_path(resource, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        resource = resource[part] if isinstance(resource, dict) else getattr(resource, part)
    if isinstance(resource, dict):
        resource[parts[-1]] = value
    else:
        setattr(resource, parts[-1], value)


@pytest.mark.parametrize("index,path,value", [
    (0, "metadata.namespace", "other"),
    (0, "metadata.owner_references", []),
    (0, "spec.scale_target_ref.name", "other"),
    (0, "spec.scale_target_ref.kind", "StatefulSet"),
    (0, "status.desired_replicas", 1),
    (0, "status.current_replicas", 1),
    (0, "status.observed_generation", 1),
    (0, "status.observed_generation", None),
    (0, "status", None),
    (1, "metadata.uid", "recreated-scaler-uid"),
    (1, "metadata.name", "other-scaler"),
    (1, "metadata.namespace", "other"),
    (1, "status.hpaName", "other-hpa"),
    (1, "spec.scaleTargetRef.name", "other"),
    (1, "spec.scaleTargetRef.kind", "StatefulSet"),
    (1, "spec.minReplicaCount", 1),
    (1, "spec.idleReplicaCount", 1),
    (1, "status.conditions", []),
    (1, "status.health", {"requests": {"status": "Failing", "numberOfFailures": 1}}),
    (2, "metadata.name", "other"),
    (2, "metadata.namespace", "other"),
    (2, "spec.replicas", 1),
    (2, "status.observed_generation", 2),
    (2, "status.observed_generation", None),
    (2, "status.replicas", 1),
    (2, "status.ready_replicas", 1),
    (2, "status.available_replicas", 1),
    (2, "status", None),
])
def test_incomplete_or_contradictory_context_cannot_prove_idle(idle_resources, index, path, value):
    _set_path(idle_resources[index], path, value)
    assert _keda_hpa_context(*idle_resources)["idle"] is False


@pytest.mark.parametrize("attribute,value", [
    ("uid", "other-uid"), ("name", "other-scaler"),
    ("controller", False), ("controller", None),
    ("kind", "Deployment"), ("api_version", "other.example/v1"),
])
def test_owner_must_be_the_matching_keda_controller(idle_resources, attribute, value):
    setattr(idle_resources[0].metadata.owner_references[0], attribute, value)
    assert _keda_hpa_context(*idle_resources)["idle"] is False


@pytest.mark.parametrize("condition,status,reason", [
    ("Ready", "False", "ScaledObjectCheckFailed"),
    ("Ready", "Unknown", ""),
    ("Active", "True", "ScalerActive"),
    ("Active", "False", "ScalerError"),
    ("Active", "Unknown", ""),
    ("Paused", "True", "ScaledObjectPaused"),
    ("Fallback", "True", "FallbackExists"),
])
def test_keda_errors_and_pause_are_not_normal_idle(idle_resources, condition, status, reason):
    conditions = idle_resources[1]["status"]["conditions"]
    matching = next(item for item in conditions if item["type"] == condition)
    matching.update(status=status, reason=reason)
    assert _keda_hpa_context(*idle_resources)["idle"] is False


@pytest.mark.parametrize("annotation,value", [
    ("autoscaling.keda.sh/paused", "true"),
    ("autoscaling.keda.sh/paused-replicas", "0"),
    ("autoscaling.keda.sh/paused-scale-in", "true"),
    ("autoscaling.keda.sh/paused-scale-out", "true"),
])
def test_pause_annotations_remain_explicit_context(idle_resources, annotation, value):
    idle_resources[1]["metadata"]["annotations"] = {annotation: value}
    assert _keda_hpa_context(*idle_resources)["idle"] is False


@pytest.mark.parametrize("condition,status,reason", [
    ("ScalingActive", "False", "FailedGetResourceMetric"),
    ("ScalingActive", "False", "FailedGetExternalMetric"),
    ("ScalingActive", "False", "FailedComputeMetricsReplicas"),
    ("ScalingActive", "Unknown", "ScalingDisabled"),
    ("AbleToScale", "False", "FailedGetScale"),
    ("AbleToScale", "False", "FailedUpdateScale"),
])
def test_hpa_scaling_failures_remain_actionable(idle_resources, condition, status, reason):
    matching = next(item for item in idle_resources[0].status.conditions if item.type == condition)
    matching.status, matching.reason = status, reason
    assert _keda_hpa_context(*idle_resources)["idle"] is False


@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_missing_resource_never_proves_idle(idle_resources, missing_index):
    resources = list(idle_resources)
    resources[missing_index] = None
    assert _keda_hpa_context(*resources)["idle"] is False


def test_hpa_without_generation_fields_can_be_idle(idle_resources):
    idle_resources[0].metadata.generation = None
    idle_resources[0].status.observed_generation = None
    assert _keda_hpa_context(*idle_resources)["idle"] is True


def test_raw_keda_failure_context_is_preserved(idle_resources):
    scaler = idle_resources[1]
    scaler["status"]["conditions"][0]["status"] = "False"
    scaler["status"]["health"] = {"requests": {"status": "Failing", "numberOfFailures": 2}}
    before = deepcopy(scaler)
    context = _keda_hpa_context(*idle_resources)
    assert context["idle"] is False
    assert context["min"] == 0
    assert context["conditions"] == scaler["status"]["conditions"]
    assert context["health"] == scaler["status"]["health"]
    assert scaler == before


@pytest.mark.parametrize("scenario", ["matched", "read_error", "non_keda"])
def test_collector_correlates_keda_or_reports_missing_evidence(monkeypatch, idle_resources, scenario):
    from tools import k8s_client
    from kubernetes.client.rest import ApiException

    hpa, scaled_object, deployment = idle_resources
    if scenario == "non_keda":
        hpa.metadata.owner_references = []
    reads = []

    def get_scaler(*args):
        reads.append(args)
        if scenario == "read_error":
            raise ApiException(status=403, reason="Forbidden")
        return scaled_object

    empty = lambda **kwargs: NS(items=[])
    monkeypatch.setattr(k8s_client, "core_v1", lambda: NS(
        list_node=empty, list_pod_for_all_namespaces=empty,
        list_event_for_all_namespaces=empty, list_persistent_volume=empty,
    ))
    monkeypatch.setattr(k8s_client, "apps_v1", lambda: NS(
        list_deployment_for_all_namespaces=lambda: NS(items=[deployment]),
    ))
    monkeypatch.setattr(k8s_client, "autoscaling_v2", lambda: NS(
        list_horizontal_pod_autoscaler_for_all_namespaces=lambda: NS(items=[hpa]),
    ))
    monkeypatch.setattr(k8s_client, "custom_objects", lambda: NS(
        list_cluster_custom_object=lambda *args: {"items": []},
        get_namespaced_custom_object=get_scaler,
    ))

    data = _collect_cluster_data()
    assert len(data["hpas"]) == 1
    assert data["hpas"][0]["conditions"][1]["reason"] == "ScalingDisabled"
    if scenario == "matched":
        assert reads == [("keda.sh", "v1alpha1", "example", "scaledobjects", "request-scaler")]
        assert data["hpas"][0]["keda"]["idle"] is True
        assert not data["errors"]
    elif scenario == "read_error":
        assert reads
        assert "keda" not in data["hpas"][0]
        assert any("KEDA example/request-scaler" in error and "Forbidden" in error
                   for error in data["errors"])
        assert "COLLECTION ERRORS" in _format_snapshot(data)
    else:
        assert not reads
        assert "keda" not in data["hpas"][0]
        assert not data["errors"]
