
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.platform_paths import RUNTIME_STATE_ENTRY_NAMES, runtime_db_paths, runtime_state_paths


def test_operational_paths_are_unqualified(tmp_path):
    db, analytics, uploads = runtime_db_paths(tmp_path)
    assert db.name == "corsia.db"
    assert analytics.name == "analytics.duckdb"
    assert uploads.name == "uploads"


def test_all_paths_rooted_at_op_dir(tmp_path):
    for path in runtime_db_paths(tmp_path):
        assert path.parent == tmp_path


def test_adoption_registry_keeps_workspace_projections_outside_the_move(tmp_path):
    paths = runtime_state_paths(tmp_path)

    assert tuple(path.name for path in paths) == RUNTIME_STATE_ENTRY_NAMES
    assert "context" not in RUNTIME_STATE_ENTRY_NAMES
    assert "skills" not in RUNTIME_STATE_ENTRY_NAMES


@pytest.mark.parametrize(
    "state_relative",
    [Path("."), Path(".corsia")],
)
def test_config_rejects_state_override_at_or_inside_workspace_outside_ci(
    monkeypatch,
    tmp_path,
    state_relative,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import infra.config as config

    try:
        with monkeypatch.context() as patch:
            patch.setenv("CORSIA_WORKSPACE", str(workspace))
            patch.setenv(
                "CORSIA_STATE_DIR",
                str(workspace / state_relative),
            )
            patch.delenv("GITHUB_ACTIONS", raising=False)
            patch.delenv("CI", raising=False)
            patch.delenv("CORSIA_TEST_MODE", raising=False)

            with pytest.raises(
                RuntimeError,
                match="state root must be outside workspace",
            ):
                importlib.reload(config)
    finally:
        importlib.reload(config)


@pytest.fixture(autouse=True)
def _restore_config_after():
    yield
    import infra.config as cfg
    importlib.reload(cfg)
