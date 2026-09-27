"""Keep image-only publishing separate from versioned chart releases."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.mark.parametrize("event,ref,valid", [
    ("workflow_dispatch", "feature/release-candidate", True),
    ("workflow_dispatch", "main", True),
    ("push", "main", True),
    ("push", "chart-version", True),
    ("push", "v999.999.999", False),
])
def test_release_image_version(event, ref, valid, tmp_path):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["release"]["steps"]
                  if step.get("id") == "version")
    version = str(yaml.safe_load((root / "chart/Chart.yaml").read_text())["version"])
    output = tmp_path / "output"
    sha = "a" * 40
    env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_SHA=sha,
               GITHUB_REF_NAME=f"v{version}" if ref == "chart-version" else ref,
               GITHUB_REF=f"refs/heads/{ref}" if event == "workflow_dispatch" or ref == "main"
               else f"refs/tags/{ref}",
               GITHUB_OUTPUT=str(output))
    result = subprocess.run(["bash", "-c", script], cwd=root, env=env,
                            capture_output=True, text=True)
    assert (result.returncode == 0) is valid
    if valid:
        expected = f"sha-{sha}" if event == "workflow_dispatch" or ref == "main" else version
        tags = [f"ghcr.io/jonfairbanks/sre-agent:{expected}"]
        if ref == "main":
            tags.append("ghcr.io/jonfairbanks/sre-agent:latest")
        assert output.read_text().splitlines() == [
            f"version={expected}", "tags<<EOF", *tags, "EOF",
        ]
    else:
        assert not output.exists()


def test_only_main_pushes_and_version_tags_trigger_release():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"] == {"branches": ["main"], "tags": ["v*.*.*"]}
    assert workflow["concurrency"] == {
        "group": "release-${{ github.ref }}", "cancel-in-progress": "false",
    }


@pytest.mark.parametrize("current", [True, False])
def test_old_main_runs_cannot_publish_latest(current, tmp_path):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Verify Main Is Still Current")
    assert guard["if"] == "github.ref == 'refs/heads/main'"
    assert steps.index(guard) < next(i for i, step in enumerate(steps) if step.get("id") == "image")
    git = tmp_path / "git"
    git.write_text('#!/bin/sh\nprintf "%s\\trefs/heads/main\\n" "$TEST_REMOTE_SHA"\n')
    git.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               GITHUB_SHA="a" * 40, TEST_REMOTE_SHA=("a" if current else "b") * 40)
    result = subprocess.run(["bash", "-c", guard["run"]], env=env,
                            capture_output=True, text=True)
    assert (result.returncode == 0) is current
