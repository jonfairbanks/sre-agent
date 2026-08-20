"""Tests for the kubectl_delete_resources_bulk guardrails.

Every case here is a deletion the agent must NOT perform. One approval click
executes the whole batch, so the refusal paths are the safety property worth
testing. No cluster needed; the Kubernetes client is stubbed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.kubernetes_write import (
    BULK_DELETE_MAX,
    PROTECTED_NAMESPACES,
    kubectl_delete_resources_bulk,
)


def target(name="web", ns="prod", rt="deployment"):
    return {"resource_type": rt, "resource_name": name, "namespace": ns}


def run_bulk(targets_json):
    """Invoke the tool with the Kubernetes client stubbed out."""
    core, apps = MagicMock(), MagicMock()
    with patch("tools.kubernetes_write.core_v1", return_value=core), \
         patch("tools.kubernetes_write.apps_v1", return_value=apps):
        out = kubectl_delete_resources_bulk.func(targets_json=targets_json)
    return out, apps


# ---------------------------------------------------------------------------
# Batch size cap
# ---------------------------------------------------------------------------

def test_batch_over_the_cap_deletes_nothing():
    payload = json.dumps([target(f"web-{i}") for i in range(BULK_DELETE_MAX + 1)])
    out, apps = run_bulk(payload)
    assert out.startswith("REFUSED:")
    assert str(BULK_DELETE_MAX) in out
    apps.delete_namespaced_deployment.assert_not_called()


def test_batch_exactly_at_the_cap_is_allowed():
    payload = json.dumps([target(f"web-{i}") for i in range(BULK_DELETE_MAX)])
    out, apps = run_bulk(payload)
    assert not out.startswith("REFUSED:")
    assert apps.delete_namespaced_deployment.call_count == BULK_DELETE_MAX


def test_refusal_tells_the_caller_what_to_do():
    payload = json.dumps([target(f"web-{i}") for i in range(BULK_DELETE_MAX + 5)])
    out, _ = run_bulk(payload)
    assert "Split the request" in out


# ---------------------------------------------------------------------------
# Protected namespaces
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ns", sorted(PROTECTED_NAMESPACES))
def test_each_protected_namespace_is_refused(ns):
    out, apps = run_bulk(json.dumps([target(ns=ns)]))
    assert out.startswith("REFUSED:")
    assert ns in out
    apps.delete_namespaced_deployment.assert_not_called()


def test_one_protected_target_blocks_the_entire_batch():
    """A partially-applied destructive batch is worse than none at all."""
    out, apps = run_bulk(json.dumps([
        target("web", "prod"),
        target("api", "staging"),
        target("coredns", "kube-system"),
    ]))
    assert out.startswith("REFUSED:")
    assert "kube-system" in out
    apps.delete_namespaced_deployment.assert_not_called()


def test_the_agents_own_namespace_is_protected():
    """The agent must not be able to delete itself."""
    assert "sre-agent" in PROTECTED_NAMESPACES
    out, apps = run_bulk(json.dumps([target("sre-agent", "sre-agent")]))
    assert out.startswith("REFUSED:")
    apps.delete_namespaced_deployment.assert_not_called()


def test_a_target_with_no_namespace_defaults_and_is_allowed():
    """Omitted namespace means "default", which is not protected."""
    out, apps = run_bulk(json.dumps([{"resource_type": "deployment", "resource_name": "web"}]))
    assert not out.startswith("REFUSED:")
    apps.delete_namespaced_deployment.assert_called_once()


def test_unprotected_namespaces_still_work():
    out, apps = run_bulk(json.dumps([target("web", "prod"), target("api", "staging")]))
    assert not out.startswith("REFUSED:")
    assert apps.delete_namespaced_deployment.call_count == 2


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------

def test_non_list_payload_is_rejected():
    out, apps = run_bulk('{"resource_type": "deployment", "resource_name": "web"}')
    assert out.startswith("ERROR: targets_json must be a JSON array")
    apps.delete_namespaced_deployment.assert_not_called()


def test_invalid_json_is_rejected():
    out, apps = run_bulk("not json at all")
    assert "Invalid JSON" in out
    apps.delete_namespaced_deployment.assert_not_called()


def test_empty_batch_is_a_noop():
    out, apps = run_bulk("[]")
    assert "No targets provided" in out
    apps.delete_namespaced_deployment.assert_not_called()
