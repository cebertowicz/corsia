
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.config import _guard_against_prod_in_ci_or_tests


def _make_bootstrap_config(tmp_path: Path, workspace: Path) -> Path:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"workspace": str(workspace)}))
    return config


def _make_v2_bootstrap_config(
    tmp_path: Path,
    workspace: Path,
    state_root: Path,
) -> Path:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-123",
                    "name": "Primary",
                    "root": str(workspace),
                },
                "state_root": str(state_root),
            }
        )
    )
    return config


def test_guard_no_op_outside_ci_and_tests(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    _guard_against_prod_in_ci_or_tests(Path.home() / ".corsia", source="test")


def test_guard_fires_under_test_mode_without_ci(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    with pytest.raises(RuntimeError, match="IS the prod workspace"):
        _guard_against_prod_in_ci_or_tests(prod_ws.resolve(), source="test")


def test_guard_test_mode_accepts_distinct_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    ephemeral = tmp_path / "corsia-test-ws-abc"
    ephemeral.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    _guard_against_prod_in_ci_or_tests(ephemeral.resolve(), source="test")


def test_guard_raises_when_workspace_equals_prod(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    with pytest.raises(RuntimeError, match="IS the prod workspace"):
        _guard_against_prod_in_ci_or_tests(prod_ws.resolve(), source="test")


def test_guard_accepts_distinct_workspace_in_ci(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    ci_ws = tmp_path / "ci-workspace"
    ci_ws.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    _guard_against_prod_in_ci_or_tests(ci_ws.resolve(), source="test")


def test_guard_runner_temp_in_ci_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = Path.home() / ".corsia"
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    fake_runner_temp = (
        Path.home() / ".corsia-runner" / "_work" / "_temp" / "corsia-ci-workspace"
    )
    _guard_against_prod_in_ci_or_tests(fake_runner_temp, source="CORSIA_WORKSPACE env")


def test_guard_respects_override_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", "1")
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    _guard_against_prod_in_ci_or_tests(prod_ws.resolve(), source="test")


def test_guard_fires_on_generic_ci_env(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    with pytest.raises(RuntimeError, match="IS the prod workspace"):
        _guard_against_prod_in_ci_or_tests(prod_ws.resolve(), source="test")


def test_guard_fallback_when_no_bootstrap_config(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    missing = tmp_path / "nonexistent.json"
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", missing)
    with pytest.raises(RuntimeError, match="no bootstrap config"):
        _guard_against_prod_in_ci_or_tests(Path.home() / ".corsia", source="test")


def test_guard_error_message_includes_source(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    prod_ws = tmp_path / "prod-workspace"
    prod_ws.mkdir()
    bootstrap = _make_bootstrap_config(tmp_path, prod_ws)
    monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", bootstrap)
    with pytest.raises(RuntimeError, match="bootstrap config"):
        _guard_against_prod_in_ci_or_tests(prod_ws.resolve(), source="bootstrap config")


def test_distinct_test_workspace_rejects_explicit_prod_state_override(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    prod_workspace = tmp_path / "prod-workspace"
    test_workspace = tmp_path / "test-workspace"
    prod_state = tmp_path / "prod-state"
    bootstrap = _make_v2_bootstrap_config(
        tmp_path,
        prod_workspace,
        prod_state,
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(test_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(prod_state))
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="prod state root"):
        config._resolve_state_root()


def test_distinct_local_workspace_rejects_explicit_prod_state_override(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    prod_workspace = tmp_path / "prod-workspace"
    selected_workspace = tmp_path / "selected-workspace"
    prod_state = tmp_path / "prod-state"
    bootstrap = _make_v2_bootstrap_config(
        tmp_path,
        prod_workspace,
        prod_state,
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(selected_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(prod_state))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="prod state root"):
        config._resolve_state_root()


def test_prod_state_escape_does_not_apply_to_a_distinct_workspace(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    prod_workspace = tmp_path / "prod-workspace"
    test_workspace = tmp_path / "test-workspace"
    prod_state = tmp_path / "prod-state"
    bootstrap = _make_v2_bootstrap_config(
        tmp_path,
        prod_workspace,
        prod_state,
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(test_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(prod_state))
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.setenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", "1")
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="prod state root"):
        config._resolve_state_root()


def test_same_workspace_can_explicitly_select_its_bootstrapped_state(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    prod_workspace = tmp_path / "prod-workspace"
    prod_state = tmp_path / "prod-state"
    bootstrap = _make_v2_bootstrap_config(
        tmp_path,
        prod_workspace,
        prod_state,
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(prod_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(prod_state))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    assert config._resolve_state_root() == prod_state.resolve()


def test_malformed_bootstrap_with_workspace_override_fails_loud_locally(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    bootstrap = tmp_path / "config.json"
    bootstrap.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("CORSIA_WORKSPACE", str(tmp_path / "selected-workspace"))
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="Failed to read"):
        config._resolve_state_root()


def test_legacy_bootstrap_does_not_misclassify_explicit_test_state_as_prod(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    prod_workspace = tmp_path / "prod-workspace"
    bootstrap = _make_bootstrap_config(tmp_path, prod_workspace)
    test_workspace = tmp_path / "test-workspace"
    explicit_state = tmp_path / "test-state"
    monkeypatch.setenv("CORSIA_WORKSPACE", str(test_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(explicit_state))
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    assert config._resolve_state_root() == explicit_state.resolve()


def test_distinct_workspace_rejects_explicit_legacy_prod_state(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    prod_workspace = tmp_path / "prod-workspace"
    selected_workspace = tmp_path / "selected-workspace"
    prod_state = prod_workspace / ".corsia"
    bootstrap = _make_bootstrap_config(tmp_path, prod_workspace)
    monkeypatch.setenv("CORSIA_WORKSPACE", str(selected_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(prod_state))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="prod state root"):
        config._resolve_state_root()


# --- Sandboxed bootstrap stat denial (EPERM, not ENOENT) -------------------
#
# A hard-denied home directory (e.g. CORSIA's agent sandbox) makes even
# `Path.exists()` raise PermissionError instead of returning False. These
# tests force that exact condition deterministically rather than relying on
# the ambient environment actually being sandboxed.


class _EPERMPath(type(Path())):
    """A Path whose ``exists()`` raises PermissionError, simulating a sandbox
    denying even a stat of the real home directory."""

    def exists(self, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(self))


def _unreadable_bootstrap_path(tmp_path: Path) -> Path:
    return _EPERMPath(str(tmp_path / "config.json"))


def test_bootstrap_config_status_tri_state(monkeypatch, tmp_path):
    import infra.config as config

    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", tmp_path / "missing.json")
    assert config._bootstrap_config_status() is False

    existing = _make_bootstrap_config(tmp_path, tmp_path / "ws")
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", existing)
    assert config._bootstrap_config_status() is True

    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))
    assert config._bootstrap_config_status() is None


def test_resolve_state_root_proceeds_when_bootstrap_unreadable_and_state_dir_set(
    monkeypatch, tmp_path
):
    import infra.config as config

    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))
    monkeypatch.setattr(config, "default_state_root", lambda: state_dir)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_dir))
    monkeypatch.delenv("CORSIA_WORKSPACE", raising=False)

    assert config._resolve_state_root() == state_dir


def test_resolve_state_root_raises_clear_error_when_bootstrap_unreadable_and_state_dir_unset(
    monkeypatch, tmp_path
):
    import infra.config as config

    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)
    monkeypatch.delenv("CORSIA_WORKSPACE", raising=False)

    with pytest.raises(RuntimeError, match="permission denied"):
        config._resolve_state_root()


def test_resolve_workspace_env_wins_even_when_bootstrap_unreadable(monkeypatch, tmp_path):
    import infra.config as config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))
    monkeypatch.setenv("CORSIA_WORKSPACE", str(workspace))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)

    assert config._resolve_workspace() == workspace.resolve()


def test_resolve_workspace_raises_clear_error_when_bootstrap_unreadable_and_unset(
    monkeypatch, tmp_path
):
    import infra.config as config

    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))
    monkeypatch.delenv("CORSIA_WORKSPACE", raising=False)

    with pytest.raises(RuntimeError, match="permission denied"):
        config._resolve_workspace()


def test_resolve_workspace_selection_falls_back_when_bootstrap_unreadable(
    monkeypatch, tmp_path
):
    import infra.config as config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))

    selection = config._resolve_workspace_selection(workspace)
    assert selection.root == workspace.resolve()


def test_full_runtime_root_resolution_survives_sandboxed_bootstrap_denial(
    monkeypatch, tmp_path
):
    """End-to-end: the exact init sequence the reported crash hit, with the
    bootstrap stat denied but full isolation env vars set."""
    import infra.config as config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", _unreadable_bootstrap_path(tmp_path))
    monkeypatch.setenv("CORSIA_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_dir))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)

    resolved_workspace, resolved_state = config._resolve_runtime_roots()
    assert resolved_workspace == workspace.resolve()
    assert resolved_state == state_dir.resolve()

    selection = config._resolve_workspace_selection(resolved_workspace)
    assert selection.root == workspace.resolve()
