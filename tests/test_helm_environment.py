"""Helm state uses writable paths without discarding caller settings."""
from types import SimpleNamespace

from tools import helm


def test_defaults_use_tmp_and_preserve_environment(monkeypatch):
    for name in ("HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KUBECONFIG", "/custom/kubeconfig")
    calls = []
    monkeypatch.setattr(helm.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    assert helm._helm("repo", "list") == (True, "ok")
    assert calls[0]["env"]["HELM_CONFIG_HOME"] == "/tmp/sre-agent-helm/config"
    assert calls[0]["env"]["HELM_CACHE_HOME"] == "/tmp/sre-agent-helm/cache"
    assert calls[0]["env"]["HELM_DATA_HOME"] == "/tmp/sre-agent-helm/data"
    assert calls[0]["env"]["KUBECONFIG"] == "/custom/kubeconfig"


def test_explicit_paths_are_respected(monkeypatch):
    for name in ("HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
        monkeypatch.setenv(name, f"/mounted/{name}")
    calls = []
    monkeypatch.setattr(helm.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    helm._helm("list")
    for name in ("HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
        assert calls[0]["env"][name] == f"/mounted/{name}"
