"""Helm read tools report transport failures and parse release responses."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tools import helm


@pytest.mark.parametrize("result,expected", [
    (SimpleNamespace(returncode=1, stdout="", stderr="  forbidden  "), "helm list failed:\nforbidden"),
    (FileNotFoundError(), "ERROR: helm CLI not found. Install helm or add it to the container image."),
    (subprocess.TimeoutExpired(["helm", "list"], 7), "ERROR: helm command timed out after 7s"),
])
def test_helm_transport_failures_are_returned_without_parsing(monkeypatch, result, expected):
    run = Mock(side_effect=result if isinstance(result, Exception) else None,
               return_value=result if not isinstance(result, Exception) else None)
    monkeypatch.setattr(helm.subprocess, "run", run)
    assert helm._helm("list", timeout=7) == (False, expected)
    assert run.call_args.args[0] == ["helm", "list"]
    assert run.call_args.kwargs["timeout"] == 7


def test_release_list_parses_json_and_passes_requested_namespace(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([
        {"name": "web", "namespace": "demo", "chart": "nginx-1.0.0", "app_version": "1.27", "status": "deployed"},
        {"name": "worker", "namespace": "demo", "chart": "jobs-2.0.0", "status": "failed"},
    ])))
    monkeypatch.setattr(helm.subprocess, "run", run)
    output = helm.helm_list_releases.invoke({"namespace": "demo"})
    assert "web" in output and "nginx-1.0.0" in output and "1.27" in output and "deployed" in output
    assert "worker" in output and "failed" in output
    assert run.call_args.args[0] == ["helm", "list", "--output", "json", "--namespace", "demo"]


@pytest.mark.parametrize("payload,expected", [("[]", "No Helm releases found in any namespace"),
                                             ("not JSON", "not JSON")])
def test_release_list_handles_empty_and_non_json_responses(monkeypatch, payload, expected):
    monkeypatch.setattr(helm.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stderr="", stdout=payload)))
    assert helm.helm_list_releases.invoke({}) == expected
