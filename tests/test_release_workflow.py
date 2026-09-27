"""Keep image-only publishing separate from versioned chart releases."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.mark.parametrize("event,ref,valid", [
    ("workflow_dispatch", "feature/release-candidate", True),
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
               GITHUB_OUTPUT=str(output))
    result = subprocess.run(["bash", "-c", script], cwd=root, env=env,
                            capture_output=True, text=True)
    assert (result.returncode == 0) is valid
    if valid:
        expected = f"sha-{sha}" if event == "workflow_dispatch" else version
        assert output.read_text().strip() == f"version={expected}"
    else:
        assert not output.exists()
