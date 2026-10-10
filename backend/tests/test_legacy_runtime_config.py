"""Runtime compatibility contracts for path-only workspace bootstraps."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_legacy_bootstrap_runtime_keeps_existing_operational_db(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    workspace = tmp_path / "legacy-workspace"
    legacy_state = workspace / ".corsia"
    legacy_db = legacy_state / "corsia.db"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    legacy_state.mkdir(parents=True)
    legacy_db.write_bytes(b"legacy-sentinel")
    bootstrap.write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(repo / "backend")
    for key in (
        "CI",
        "GITHUB_ACTIONS",
        "CORSIA_STATE_DIR",
        "CORSIA_TEST_MODE",
        "CORSIA_WORKSPACE",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from infra import config; "
                "print(json.dumps({'state': str(config.STATE_ROOT), "
                "'db': str(config.DB_PATH)}))"
            ),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved == {
        "state": str(legacy_state.resolve()),
        "db": str(legacy_db.resolve()),
    }
    assert legacy_db.read_bytes() == b"legacy-sentinel"


def test_v2_bootstrap_still_rejects_overlapping_legacy_state(tmp_path):
    from infra.platform_paths import load_bootstrap

    workspace = tmp_path / "workspace"
    bootstrap = tmp_path / "config.json"
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-123",
                    "name": "Primary",
                    "root": str(workspace),
                },
                "state_root": str(workspace / ".corsia"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not overlap"):
        load_bootstrap(bootstrap)


@pytest.mark.parametrize(
    "select_same_workspace",
    [False, True],
    ids=["bootstrap-selection", "matching-workspace-env"],
)
def test_legacy_selected_workspace_rejects_distinct_state_override_before_g4(
    tmp_path,
    monkeypatch,
    select_same_workspace,
):
    import infra.config as config

    workspace = tmp_path / "legacy-workspace"
    legacy_state = workspace / ".corsia"
    legacy_db = legacy_state / "corsia.db"
    override_state = tmp_path / "new-platform-state"
    bootstrap = tmp_path / "config.json"
    legacy_state.mkdir(parents=True)
    legacy_db.write_bytes(b"legacy-sentinel")
    bootstrap.write_text(
        json.dumps({"workspace": str(workspace)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(override_state))
    if select_same_workspace:
        monkeypatch.setenv("CORSIA_WORKSPACE", str(workspace))
    else:
        monkeypatch.delenv("CORSIA_WORKSPACE", raising=False)

    with pytest.raises(RuntimeError, match="legacy bootstrap"):
        config._resolve_state_root()

    assert legacy_db.read_bytes() == b"legacy-sentinel"
    assert not override_state.exists()
