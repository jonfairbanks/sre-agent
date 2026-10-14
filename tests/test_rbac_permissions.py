"""Render deployment roles and check allowed workload and denied authority paths."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def allowed(rules, group, resource, verb):
    return any(group in r.get("apiGroups", []) and resource in r.get("resources", [])
               and verb in r.get("verbs", []) for r in rules)


def render(writer=False):
    if not shutil.which("helm"):
        pytest.skip("Helm is required to exercise rendered RBAC")
    text = subprocess.check_output([
        "helm", "template", "sre-agent", str(ROOT / "chart"), "--namespace", "sre-agent",
        "--set", "rbac.write.create=" + str(writer).lower(),
    ], text=True)
    return {d["metadata"]["name"]: d["rules"] for d in yaml.safe_load_all(text)
            if d and d.get("kind") == "ClusterRole"}


def reader_rules():
    return [yaml.safe_load((ROOT / "k8s/clusterrole.yaml").read_text())["rules"],
            render()["sre-agent-reader"]]


def writer_rules():
    return [yaml.safe_load((ROOT / "k8s/role-writer.yaml").read_text())["rules"],
            render(True)["sre-agent-writer"]]


def test_no_default_or_optional_role_has_wildcard_authority():
    for rules in reader_rules() + writer_rules():
        for rule in rules:
            assert all("*" not in rule.get(k, []) for k in ("apiGroups", "resources", "verbs"))


@pytest.mark.parametrize("resource", ["secrets", "serviceaccounts/token"])
def test_secret_material_is_not_readable(resource):
    for rules in reader_rules() + writer_rules():
        for verb in ("get", "list", "watch"):
            assert not allowed(rules, "", resource, verb)


def test_registered_observation_paths_remain_readable():
    for rules in reader_rules():
        for group, resource, verb in [
            ("", "pods", "watch"), ("", "pods/log", "get"), ("", "nodes/proxy", "get"),
            ("", "configmaps", "get"), ("", "endpoints", "list"), ("", "limitranges", "list"),
            ("storage.k8s.io", "storageclasses", "get"),
            ("apps", "deployments", "get"), ("argoproj.io", "applications", "list"),
            ("secrets.hashicorp.com", "vaultstaticsecrets", "get"),
        ]:
            assert allowed(rules, group, resource, verb), (group, resource, verb)


def test_registered_workload_mutations_remain_allowed_when_writer_enabled():
    for rules in writer_rules():
        for group, resource, verb in [
            ("apps", "deployments", "create"), ("apps", "daemonsets", "delete"),
            ("apps", "statefulsets", "patch"), ("apps", "replicasets", "delete"),
            ("apps", "deployments/scale", "patch"), ("", "pods", "delete"),
            ("", "persistentvolumeclaims", "patch"), ("", "persistentvolumeclaims", "delete"),
            ("", "configmaps", "create"), ("", "services", "patch"),
            ("networking.k8s.io", "ingresses", "create"),
            ("autoscaling", "horizontalpodautoscalers", "patch"),
        ]:
            assert allowed(rules, group, resource, verb), (group, resource, verb)


def test_authority_and_unconfigured_custom_resource_mutations_are_denied():
    for rules in reader_rules() + writer_rules():
        for group, resource, verb in [
            ("rbac.authorization.k8s.io", "clusterrolebindings", "create"),
            ("rbac.authorization.k8s.io", "clusterroles", "escalate"),
            ("", "users", "impersonate"), ("", "secrets", "patch"),
            ("example.org", "customobjects", "delete"),
        ]:
            assert not allowed(rules, group, resource, verb)


def test_default_chart_does_not_enable_writer():
    assert "sre-agent-writer" not in render()
