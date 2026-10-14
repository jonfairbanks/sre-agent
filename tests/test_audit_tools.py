"""Audit tools distinguish configured workloads from actionable findings."""
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from tools import kubernetes_hygiene as hygiene
from tools import kubernetes_security as security


def metadata(name, namespace="demo", labels=None):
    return client.V1ObjectMeta(name=name, namespace=namespace, labels=labels)


def test_rbac_flags_wildcards_and_admin_bindings_without_flagging_explicit_reader(monkeypatch):
    api = Mock()
    api.list_cluster_role.return_value.items = [
        client.V1ClusterRole(metadata=metadata("pod-reader"), rules=[
            client.V1PolicyRule(api_groups=[""], resources=["pods", "pods/log"], verbs=["get", "list", "watch"]),
        ]),
        client.V1ClusterRole(metadata=metadata("all-resources"), rules=[
            client.V1PolicyRule(api_groups=[""], resources=["*"], verbs=["get"]),
        ]),
        client.V1ClusterRole(metadata=metadata("all-verbs"), rules=[
            client.V1PolicyRule(api_groups=["apps"], resources=["deployments"], verbs=["*"]),
        ]),
    ]
    api.list_cluster_role_binding.return_value.items = [
        client.V1ClusterRoleBinding(metadata=metadata("operators"),
            role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="cluster-admin"),
            subjects=[client.RbacV1Subject(kind="Group", name="operators")]),
    ]
    api.list_role_binding_for_all_namespaces.return_value.items = []
    monkeypatch.setattr(security, "rbac_v1", lambda: api)
    output = security.kubectl_get_rbac_summary.invoke({})
    assert "*** CLUSTER-ADMIN ***" in output
    assert "Group/operators" in output
    assert "all-resources:" in output and "all-verbs:" in output
    assert "pod-reader:" not in output


def pod(name, security_context, *, init_containers=None):
    return client.V1Pod(metadata=metadata(name), spec=client.V1PodSpec(
        containers=[client.V1Container(name="app", security_context=security_context)],
        init_containers=init_containers,
    ))


def test_pod_security_false_flags_are_safe_and_privileged_init_container_is_reported(monkeypatch):
    safe = client.V1SecurityContext(privileged=False, allow_privilege_escalation=False,
                                    run_as_non_root=True, run_as_user=1000)
    api = Mock()
    api.list_namespaced_pod.return_value.items = [pod("hardened", safe), pod("bootstrap", safe, init_containers=[
        client.V1Container(name="setup", security_context=client.V1SecurityContext(privileged=True)),
    ])]
    monkeypatch.setattr(security, "core_v1", lambda: api)
    output = security.kubectl_audit_pod_security.invoke({"namespace": "demo"})
    assert "demo/hardened" not in output
    assert "demo/bootstrap" in output and "container 'setup': privileged=true" in output
    assert "allowPrivilegeEscalation=true" not in output
    api.list_namespaced_pod.assert_called_once_with("demo")


def test_missing_security_context_is_reported_in_cluster_scan(monkeypatch):
    api = Mock()
    api.list_pod_for_all_namespaces.return_value.items = [pod("unconfigured", None)]
    monkeypatch.setattr(security, "core_v1", lambda: api)
    output = security.kubectl_audit_pod_security.invoke({"namespace": "--all-namespaces"})
    assert "demo/unconfigured" in output and "no securityContext" in output
    api.list_namespaced_pod.assert_not_called()


def deployment(name, resources):
    return client.V1Deployment(metadata=metadata(name), spec=client.V1DeploymentSpec(
        selector=client.V1LabelSelector(match_labels={"app": name}),
        template=client.V1PodTemplateSpec(spec=client.V1PodSpec(
            containers=[client.V1Container(name="app", resources=resources)],
        )),
    ))


def test_resource_audit_keeps_configured_workloads_out_of_findings(monkeypatch):
    api = Mock()
    api.list_namespaced_deployment.return_value.items = [
        deployment("configured", client.V1ResourceRequirements(requests={"cpu": "100m", "memory": "64Mi"},
                                                              limits={"cpu": "500m", "memory": "128Mi"})),
        deployment("missing", None),
    ]
    api.list_namespaced_stateful_set.return_value.items = []
    monkeypatch.setattr(hygiene, "apps_v1", lambda: api)
    output = hygiene.kubectl_audit_missing_limits.invoke({"namespace": "demo"})
    assert "MISSING BOTH REQUESTS AND LIMITS (1)" in output
    assert "Deployment demo/missing" in output
    assert "demo/configured" not in output


def service(name, selector=None, type="ClusterIP"):
    return client.V1Service(metadata=metadata(name), spec=client.V1ServiceSpec(selector=selector, type=type))


def test_service_audit_accepts_extra_pod_labels_and_intentional_external_service(monkeypatch):
    api = Mock()
    api.list_namespaced_service.return_value.items = [
        service("matching", {"app": "api", "tier": "web"}),
        service("wrong-tier", {"app": "api", "tier": "worker"}),
        service("external-db", type="ExternalName"),
    ]
    api.list_namespaced_pod.return_value.items = [client.V1Pod(metadata=metadata(
        "api", labels={"app": "api", "tier": "web", "version": "v2"}))]
    monkeypatch.setattr(hygiene, "core_v1", lambda: api)
    output = hygiene.kubectl_audit_selector_mismatch.invoke({"namespace": "demo"})
    assert "no matching pods (1)" in output and "demo/wrong-tier" in output
    assert "demo/matching" not in output and "external-db" not in output
    assert all(call.args == ("demo",) for call in api.list_namespaced_pod.call_args_list)


@pytest.mark.parametrize("module,tool_name,client_factory,method", [
    (security, "kubectl_get_rbac_summary", "rbac_v1", "list_cluster_role_binding"),
    (hygiene, "kubectl_audit_missing_limits", "apps_v1", "list_namespaced_deployment"),
])
def test_denied_audit_does_not_report_a_clean_result(monkeypatch, module, tool_name, client_factory, method):
    api = Mock()
    getattr(api, method).side_effect = ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(module, client_factory, lambda: api)
    assert getattr(module, tool_name).invoke({}) == "ERROR [403]: Forbidden"
