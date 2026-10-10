"""Config integration contracts for selected workspace and platform state."""

from __future__ import annotations

import importlib

import pytest


def _platform_paths():
    return importlib.import_module("infra.platform_paths")


def test_config_keeps_workspace_and_operational_state_distinct(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    workspace.mkdir()

    import infra.config as config

    with monkeypatch.context() as patch:
        patch.setenv("CORSIA_WORKSPACE", str(workspace))
        patch.setenv("CORSIA_STATE_DIR", str(state_root))
        config = importlib.reload(config)

        assert config.WORKSPACE == workspace.resolve()
        assert config.STATE_ROOT == state_root.resolve()
        assert config.DB_PATH == state_root.resolve() / "corsia.db"
        assert (
            config.ANALYTICS_DB_PATH
            == state_root.resolve() / "analytics.duckdb"
        )
        assert config.UPLOADS_DIR == state_root.resolve() / "uploads"
        assert config.LOGS_DIR == state_root.resolve() / "logs"
        assert config.RUNTIME_DIR == state_root.resolve() / "runtime"

    importlib.reload(config)


def test_config_rejects_workspace_nested_inside_state_root(
    monkeypatch,
    tmp_path,
):
    import infra.config as config

    state_root = tmp_path / "state"
    monkeypatch.setenv("CORSIA_WORKSPACE", str(state_root / "workspace"))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(state_root))

    with pytest.raises(RuntimeError, match="must not overlap"):
        config._resolve_runtime_roots()


def test_workspace_env_wins_over_bootstrap_selection(
    monkeypatch,
    tmp_path,
):
    paths = _platform_paths()
    import infra.config as config

    bootstrap = tmp_path / "config.json"
    bootstrapped_workspace = tmp_path / "bootstrapped-workspace"
    env_workspace = tmp_path / "env-workspace"
    paths.write_bootstrap(
        bootstrap,
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=bootstrapped_workspace,
                name="Primary",
            ),
            state_root=tmp_path / "state",
        ),
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(env_workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(tmp_path / "test-state"))
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    assert config._resolve_workspace() == env_workspace.resolve()


def test_state_env_wins_over_bootstrap_state(
    monkeypatch,
    tmp_path,
):
    paths = _platform_paths()
    import infra.config as config

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    env_state = tmp_path / "env-state"
    paths.write_bootstrap(
        bootstrap,
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=workspace,
                name="Primary",
            ),
            state_root=tmp_path / "bootstrapped-state",
        ),
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(env_state))
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    assert config._resolve_state_root() == env_state.resolve()


def test_config_honors_bootstrapped_state_with_workspace_env(
    monkeypatch,
    tmp_path,
):
    paths = _platform_paths()
    import infra.config as config

    bootstrap = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "bootstrapped-state"
    paths.write_bootstrap(
        bootstrap,
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=workspace,
                name="Primary",
            ),
            state_root=state_root,
        ),
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(workspace))
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    assert config._resolve_state_root() == state_root.resolve()


@pytest.mark.parametrize(
    "runtime_env",
    [
        {},
        {"CI": "true"},
        {"CORSIA_TEST_MODE": "1"},
        {"CORSIA_ALLOW_HOME_WORKSPACE_IN_CI": "1"},
    ],
    ids=["local", "ci", "test", "escape-does-not-invent-state"],
)
def test_distinct_workspace_requires_explicit_state_override_in_every_mode(
    monkeypatch,
    tmp_path,
    runtime_env,
):
    paths = _platform_paths()
    import infra.config as config

    bootstrap = tmp_path / "config.json"
    prod_workspace = tmp_path / "prod-workspace"
    test_workspace = tmp_path / "test-workspace"
    paths.write_bootstrap(
        bootstrap,
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=prod_workspace,
                name="Primary",
            ),
            state_root=tmp_path / "prod-state",
        ),
    )
    monkeypatch.setenv("CORSIA_WORKSPACE", str(test_workspace))
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)
    for key in (
        "GITHUB_ACTIONS",
        "CI",
        "CORSIA_TEST_MODE",
        "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in runtime_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="CORSIA_STATE_DIR"):
        config._resolve_state_root()


def test_v2_bootstrap_prod_equality_guard_still_fails_loud(
    monkeypatch,
    tmp_path,
):
    paths = _platform_paths()
    import infra.config as config

    bootstrap = tmp_path / "config.json"
    prod_workspace = tmp_path / "prod-workspace"
    paths.write_bootstrap(
        bootstrap,
        paths.RuntimePaths(
            workspace=paths.WorkspaceSelection(
                workspace_id="workspace-123",
                root=prod_workspace,
                name="Primary",
            ),
            state_root=tmp_path / "prod-state",
        ),
    )
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv("CORSIA_ALLOW_HOME_WORKSPACE_IN_CI", raising=False)
    monkeypatch.setattr(config, "BOOTSTRAP_CONFIG_PATH", bootstrap)

    with pytest.raises(RuntimeError, match="IS the prod workspace"):
        config._guard_against_prod_in_ci_or_tests(
            prod_workspace.resolve(),
            source="v2 bootstrap",
        )
