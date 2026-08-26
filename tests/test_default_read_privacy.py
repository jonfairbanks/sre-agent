"""Regression tests for the model-facing read boundary."""
from __future__ import annotations

from pathlib import Path
import subprocess

import yaml

from tools import READ_TOOLS


def test_model_read_tools_exclude_sensitive_configuration():
    exposed = {tool.name for tool in READ_TOOLS}

    assert exposed.isdisjoint(
        {
            "kubectl_get_configmaps",
            "kubectl_get_configmap",
            "kubectl_get_custom_resources",
            "helm_list_releases",
            "helm_get_release_values",
            "helm_get_release_manifest",
            "helm_search_chart_versions",
            "helm_list_repos",
            "helm_check_for_updates",
            "helm_release_history",
        }
    )


def test_default_reader_role_has_no_secret_or_configuration_wildcards():
    chart = Path(__file__).parents[1] / "chart"
    rendered = subprocess.run(
        ["helm", "template", "privacy-test", str(chart)],
        check=True,
        capture_output=True,
        text=True,
    )
    reader = next(
        resource
        for resource in yaml.safe_load_all(rendered.stdout)
        if resource
        and resource.get("kind") == "ClusterRole"
        and resource.get("metadata", {}).get("name", "").endswith("-reader")
    )
    rules = reader["rules"]
    resources = {resource for rule in rules for resource in rule.get("resources", [])}
    api_groups = {group for rule in rules for group in rule.get("apiGroups", [])}

    assert "*" not in resources
    assert "*" not in api_groups
    assert resources.isdisjoint({"secrets", "configmaps", "serviceaccounts"})
