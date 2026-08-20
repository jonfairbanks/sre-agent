"""Tests for kubectl_resize_pvc.

These cover the refusal paths, which are the whole point of the tool. Every
case here is a resize the agent must NOT perform, verified without a cluster by
stubbing the Kubernetes client.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.kubernetes_write import (
    PVC_MAX_GROWTH_FACTOR,
    _storage_ki,
    kubectl_resize_pvc,
)


def fake_pvc(size="10Gi", storage_class="gp3"):
    pvc = MagicMock()
    pvc.spec.resources.requests = {"storage": size}
    pvc.spec.storage_class_name = storage_class
    return pvc


def run_resize(name="data-0", ns="prod", new_size="20Gi",
               current="10Gi", storage_class="gp3", expandable=True):
    """Invoke the tool with the Kubernetes client stubbed out."""
    core = MagicMock()
    core.read_namespaced_persistent_volume_claim.return_value = fake_pvc(current, storage_class)
    sc = MagicMock()
    sc.allow_volume_expansion = expandable
    storage_api = MagicMock()
    storage_api.read_storage_class.return_value = sc

    with patch("tools.kubernetes_write.core_v1", return_value=core), \
         patch("tools.kubernetes_write.k8s_client.StorageV1Api", return_value=storage_api):
        out = kubectl_resize_pvc.func(pvc_name=name, namespace=ns, new_size=new_size)
    return out, core


# ---------------------------------------------------------------------------
# Storage quantity parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("10Gi", 10 * 1024 ** 2),
    ("500Mi", 500 * 1024),
    ("2Ti", 2 * 1024 ** 3),
    ("1024Ki", 1024),
    ("10G", None),      # decimal suffix deliberately unsupported, never guessed
    ("20 gigs", None),
    ("", None),
    (None, None),
])
def test_storage_ki(raw, expected):
    assert _storage_ki(raw) == expected


# ---------------------------------------------------------------------------
# PVC resize: the happy path
# ---------------------------------------------------------------------------

def test_growth_patches_only_the_storage_request():
    out, core = run_resize(new_size="20Gi", current="10Gi")
    assert "Resized PVC prod/data-0: 10Gi -> 20Gi" in out
    args = core.patch_namespaced_persistent_volume_claim.call_args[0]
    assert args[0] == "data-0" and args[1] == "prod"
    # Exactly one field, so no other spec key can be altered.
    assert args[2] == {"spec": {"resources": {"requests": {"storage": "20Gi"}}}}


def test_result_warns_that_expansion_is_asynchronous():
    out, _ = run_resize()
    assert "asynchronous" in out
    assert "pod must" in out


# ---------------------------------------------------------------------------
# PVC resize: refusals
# ---------------------------------------------------------------------------

def test_shrink_is_refused_without_calling_the_api():
    out, core = run_resize(new_size="5Gi", current="10Gi")
    assert out.startswith("REFUSED: cannot shrink")
    core.patch_namespaced_persistent_volume_claim.assert_not_called()


def test_no_op_resize_is_reported_not_applied():
    out, core = run_resize(new_size="10Gi", current="10Gi")
    assert "No change" in out
    core.patch_namespaced_persistent_volume_claim.assert_not_called()


def test_implausible_growth_is_refused():
    """A 10Gi -> 10Ti typo would provision 1000x the storage and the bill."""
    out, core = run_resize(new_size="10Ti", current="10Gi")
    assert out.startswith("REFUSED:")
    assert f"{PVC_MAX_GROWTH_FACTOR}x" in out
    core.patch_namespaced_persistent_volume_claim.assert_not_called()


def test_growth_at_the_factor_limit_is_allowed():
    out, core = run_resize(new_size="100Gi", current="10Gi")   # exactly 10x
    assert "Resized PVC" in out
    core.patch_namespaced_persistent_volume_claim.assert_called_once()


def test_non_expandable_storage_class_is_refused():
    out, core = run_resize(expandable=False)
    assert "allowVolumeExpansion" in out
    assert out.startswith("REFUSED:")
    core.patch_namespaced_persistent_volume_claim.assert_not_called()


def test_unparseable_size_is_refused():
    out, core = run_resize(new_size="20 gigabytes")
    assert out.startswith("ERROR: could not parse new_size")
    core.patch_namespaced_persistent_volume_claim.assert_not_called()
