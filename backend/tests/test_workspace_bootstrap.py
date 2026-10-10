from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _platform_paths():
    return importlib.import_module("infra.platform_paths")


def _runtime_paths(
    module,
    *,
    workspace_id: str,
    root: Path,
    name: str,
    state_root: Path,
):
    return module.RuntimePaths(
        workspace=module.WorkspaceSelection(
            workspace_id=workspace_id,
            root=root,
            name=name,
        ),
        state_root=state_root,
    )


def test_v2_bootstrap_round_trip(tmp_path):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    runtime = _runtime_paths(
        paths,
        workspace_id="workspace-123",
        root=tmp_path / "workspace",
        name="Primary",
        state_root=tmp_path / "state",
    )

    paths.write_bootstrap(bootstrap, runtime)

    assert paths.load_bootstrap(bootstrap) == runtime
    assert json.loads(bootstrap.read_text()) == {
        "version": 2,
        "workspace": {
            "id": "workspace-123",
            "name": "Primary",
            "root": str(tmp_path / "workspace"),
        },
        "state_root": str(tmp_path / "state"),
    }


def test_legacy_one_field_bootstrap_has_stable_derived_identity(tmp_path):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "legacy-workspace"
    bootstrap.write_text(json.dumps({"workspace": str(workspace)}))

    first = paths.load_bootstrap(bootstrap)
    second = paths.load_bootstrap(bootstrap)

    assert first == second
    assert isinstance(first, paths.LegacyRuntimePaths)
    assert first.workspace.root == workspace.resolve()
    assert first.workspace.name == "legacy-workspace"
    assert first.workspace.workspace_id
    assert first.state_root == (workspace / ".corsia").resolve()


def test_legacy_bootstrap_cannot_be_persisted_as_overlapping_v2_state(tmp_path):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "legacy-workspace"
    bootstrap.write_text(json.dumps({"workspace": str(workspace)}))
    before = bootstrap.read_bytes()
    legacy = paths.load_bootstrap(bootstrap)

    with pytest.raises(TypeError, match="legacy"):
        paths.write_bootstrap(bootstrap, legacy)

    assert bootstrap.read_bytes() == before


def test_v2_root_rewrite_preserves_stable_selection_identity(tmp_path):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    state_root = tmp_path / "state"
    original = _runtime_paths(
        paths,
        workspace_id="workspace-123",
        root=tmp_path / "original-workspace",
        name="Primary",
        state_root=state_root,
    )
    paths.write_bootstrap(bootstrap, original)
    moved = paths.RuntimePaths(
        workspace=paths.WorkspaceSelection(
            workspace_id=original.workspace.workspace_id,
            root=tmp_path / "moved-workspace",
            name=original.workspace.name,
        ),
        state_root=original.state_root,
    )

    paths.write_bootstrap(bootstrap, moved)

    assert paths.load_bootstrap(bootstrap) == moved
    assert moved.workspace.workspace_id == original.workspace.workspace_id


def test_atomic_write_replaces_complete_bootstrap(tmp_path, monkeypatch):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    old_runtime = _runtime_paths(
        paths,
        workspace_id="workspace-123",
        root=tmp_path / "old-workspace",
        name="Primary",
        state_root=tmp_path / "old-state",
    )
    new_runtime = _runtime_paths(
        paths,
        workspace_id="workspace-123",
        root=tmp_path / "new-workspace",
        name="Primary",
        state_root=tmp_path / "new-state",
    )
    paths.write_bootstrap(bootstrap, old_runtime)
    old_payload = bootstrap.read_text()
    real_replace = paths.os.replace
    observed_temp: Path | None = None

    def inspect_replace(source, destination):
        nonlocal observed_temp
        observed_temp = Path(source)
        assert Path(destination) == bootstrap
        assert bootstrap.read_text() == old_payload
        assert observed_temp.parent == bootstrap.parent
        assert observed_temp.read_text() != old_payload
        real_replace(source, destination)

    monkeypatch.setattr(paths.os, "replace", inspect_replace)

    paths.write_bootstrap(bootstrap, new_runtime)

    assert observed_temp is not None
    assert not observed_temp.exists()
    assert paths.load_bootstrap(bootstrap) == new_runtime


def test_atomic_write_failure_preserves_old_bootstrap(tmp_path, monkeypatch):
    paths = _platform_paths()
    bootstrap = tmp_path / "config.json"
    old_runtime = _runtime_paths(
        paths,
        workspace_id="workspace-123",
        root=tmp_path / "old-workspace",
        name="Primary",
        state_root=tmp_path / "old-state",
    )
    new_runtime = _runtime_paths(
        paths,
        workspace_id="workspace-123",
        root=tmp_path / "new-workspace",
        name="Primary",
        state_root=tmp_path / "new-state",
    )
    paths.write_bootstrap(bootstrap, old_runtime)
    old_payload = bootstrap.read_text()

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(paths.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        paths.write_bootstrap(bootstrap, new_runtime)

    assert bootstrap.read_text() == old_payload
    assert list(tmp_path.iterdir()) == [bootstrap]
