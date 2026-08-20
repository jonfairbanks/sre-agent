"""Tests for PVC utilization collection and rendering.

The scheduled collector had no PVC data at all, so a filling volume was invisible
until pods started failing. These cover the rendering thresholds and the
degradation path, since a kubelet that refuses the proxy must not break the check.
"""
from __future__ import annotations

import pytest

from scheduler import PVC_USAGE_ALERT_PERCENT, _fmt_bytes, _format_snapshot


def base_data(**over):
    data = {
        "nodes": [{"name": "n1", "status": "Ready", "version": "v1.30"}],
        "pods": [{"namespace": "prod", "name": "api-1", "status": "Running",
                  "restarts": 0, "age": "3d"}],
        "unhealthy_pods": [],
        "events": [],
        "hpas": [],
        "deployments": [],
        "node_metrics": [],
        "pod_metrics": {},
        "pvc_usage": {},
        "errors": [],
    }
    data.update(over)
    return data


def usage(percent, used=None, capacity=10 * 1024 ** 3):
    return {
        "used_bytes": used if used is not None else int(capacity * percent / 100),
        "capacity_bytes": capacity,
        "percent": percent,
        "inodes_used": 1000,
        "inodes_free": 9000,
    }


# ---------------------------------------------------------------------------
# Byte formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "?"),
    (512, "512B"),
    (5 * 1024 ** 2, "5.0Mi"),
    (int(1.5 * 1024 ** 3), "1.5Gi"),
    (2 * 1024 ** 4, "2.0Ti"),
])
def test_fmt_bytes(raw, expected):
    assert _fmt_bytes(raw) == expected


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_section_absent_when_no_pvc_data_was_collected():
    """Degradation: no kubelet access must look like the old output, not an error."""
    assert "PVC UTILIZATION" not in _format_snapshot(base_data())


def test_all_healthy_volumes_render_as_a_single_reassuring_line():
    data = base_data(pvc_usage={f"prod/pvc-{i}": usage(5.0) for i in range(10)})
    out = _format_snapshot(data)
    assert f"=== PVC UTILIZATION === all 10 volumes below {PVC_USAGE_ALERT_PERCENT}%" in out
    # No per-volume lines, so a healthy cluster does not crowd out the report.
    assert "prod/pvc-0" not in out


def test_filling_volume_is_listed_with_used_of_capacity():
    data = base_data(pvc_usage={
        "prod/data-0": usage(91.4, capacity=48 * 1024 ** 3),
        "prod/quiet-0": usage(3.0),
    })
    out = _format_snapshot(data)
    assert f"=== PVC UTILIZATION (>={PVC_USAGE_ALERT_PERCENT}%, 1 of 2 volumes) ===" in out
    assert "prod/data-0" in out
    assert "91.4%" in out
    assert "Gi/" in out            # used of capacity, not a bare percentage
    assert "prod/quiet-0" not in out   # below threshold, omitted


def test_volumes_are_ordered_fullest_first():
    data = base_data(pvc_usage={
        "prod/a": usage(75.0),
        "prod/b": usage(99.1),
        "prod/c": usage(88.0),
    })
    out = _format_snapshot(data)
    section = out.split("PVC UTILIZATION")[1]
    assert section.index("prod/b") < section.index("prod/c") < section.index("prod/a")


def test_volume_exactly_at_the_threshold_is_reported():
    """A volume at the threshold is a risk, not a pass."""
    data = base_data(pvc_usage={"prod/edge": usage(float(PVC_USAGE_ALERT_PERCENT))})
    out = _format_snapshot(data)
    assert "prod/edge" in out
    assert "1 of 1 volumes" in out


def test_volume_just_below_the_threshold_is_summarised_only():
    data = base_data(pvc_usage={"prod/edge": usage(PVC_USAGE_ALERT_PERCENT - 0.1)})
    out = _format_snapshot(data)
    assert "prod/edge" not in out
    assert "all 1 volumes below" in out


def test_kubelet_failure_surfaces_as_a_collection_error():
    """One unreachable kubelet must not fail the whole scheduled check."""
    data = base_data(errors=["pvc usage (n1): 403 Forbidden"])
    out = _format_snapshot(data)
    assert "=== COLLECTION ERRORS ===" in out
    assert "pvc usage (n1)" in out
    assert "PVC UTILIZATION" not in out


def test_pvc_section_does_not_disturb_the_other_sections():
    data = base_data(
        pvc_usage={"prod/data-0": usage(95.0)},
        node_metrics=[{"name": "n1", "cpu": "100m", "memory": "1Gi"}],
        hpas=[{"namespace": "prod", "name": "api", "min": 1, "max": 4,
               "current": 2, "desired": 2}],
    )
    out = _format_snapshot(data)
    for section in ("=== NODES ===", "=== NODE UTILIZATION ===",
                    "=== PVC UTILIZATION", "=== HPAs ==="):
        assert section in out
    # PVC sits between node utilization and HPAs.
    assert out.index("NODE UTILIZATION") < out.index("PVC UTILIZATION") < out.index("=== HPAs ===")
