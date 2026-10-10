"""Tests for the general hosted-catalog snapshot loader.

The loader is the reusable boot-safe pattern (HMC2) that connectors / model-profiles
will share — so its contract is tested independently of the cloud-models consumer:
lazy + cached reads, fail-loud on a missing/corrupt section, never crashing at import.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

import json  # noqa: E402
import pytest  # noqa: E402

from llm.hosted_catalog import loader  # noqa: E402
from llm.hosted_catalog.loader import CatalogSnapshotError, load_section  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    loader._reset_cache_for_tests()
    yield
    loader._reset_cache_for_tests()


def test_vendored_cloud_models_section_loads():
    section = load_section("cloud-models")
    assert set(section.keys()) == {"anthropic", "openai"}
    assert section["anthropic"]["provider"] == "anthropic"
    assert section["openai"]["families"]  # non-empty ordered list


def test_section_is_cached(monkeypatch):
    first = load_section("cloud-models")
    # Point the snapshot dir somewhere invalid; cache must serve the prior read.
    monkeypatch.setattr(loader, "_SNAPSHOT_DIR", loader._SNAPSHOT_DIR / "nope")
    assert load_section("cloud-models") is first


def test_missing_section_fails_loud_with_repair_guidance():
    with pytest.raises(CatalogSnapshotError) as exc:
        load_section("connectors-not-vendored-yet")
    assert "sync_catalog_snapshot" in str(exc.value)


def test_corrupt_file_fails_loud(tmp_path, monkeypatch):
    section_dir = tmp_path / "broken"
    section_dir.mkdir()
    (section_dir / "bad.json").write_text("{ this is not json")
    monkeypatch.setattr(loader, "_SNAPSHOT_DIR", tmp_path)
    with pytest.raises(CatalogSnapshotError) as exc:
        load_section("broken")
    assert "sync_catalog_snapshot" in str(exc.value)


def test_import_does_not_read_snapshot(monkeypatch):
    """Boot-safety: importing + reimporting the package never touches the disk."""
    import importlib
    loader._reset_cache_for_tests()
    monkeypatch.setattr(loader, "_SNAPSHOT_DIR", loader._SNAPSHOT_DIR / "definitely-missing")
    # Re-importing must not raise even though the snapshot dir is now invalid —
    # nothing is read until load_section() is actually called.
    importlib.reload(loader)


def test_snapshot_version_is_reported():
    assert loader.snapshot_version() != "unknown"
