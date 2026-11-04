"""Regression tests for Kubernetes security-audit scoping."""
from types import SimpleNamespace as NS

from tools import kubernetes_security


def test_network_policy_audit_excludes_system_namespaces(monkeypatch):
    namespaces = [
        NS(metadata=NS(name=name))
        for name in ("default", "kube-system", "kube-public", "kube-node-lease", "apps")
    ]
    policies = [
        NS(metadata=NS(namespace="protected", name="default-deny"), spec=NS(pod_selector=None))
    ]

    monkeypatch.setattr(
        kubernetes_security,
        "core_v1",
        lambda: NS(list_namespace=lambda: NS(items=namespaces)),
    )
    monkeypatch.setattr(
        kubernetes_security,
        "networking_v1",
        lambda: NS(list_network_policy_for_all_namespaces=lambda: NS(items=policies)),
    )

    result = kubernetes_security.kubectl_get_network_policies.invoke(
        {"namespace": "--all-namespaces"}
    )

    assert "WORKLOAD NAMESPACES WITH NO NETWORK POLICIES (1)" in result
    assert "apps  *** NO NETWORK POLICIES" in result
    assert "kube-system  *** NO NETWORK POLICIES" not in result
    assert "default  *** NO NETWORK POLICIES" not in result
