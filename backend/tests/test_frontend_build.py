"""Fail-closed startup adapter for frontend/backend release pairing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from infra import frontend_build, startup_prerequisites


@pytest.mark.parametrize("script_name", ["e2e-local", "eval-local"])
def test_isolated_shell_launchers_mark_backend_as_test_mode(script_name: str) -> None:
    """Repository-owned worktree launchers use the canonical isolation signal."""
    script = Path(__file__).parents[2] / "scripts" / script_name
    launch = script.read_text().split("boot_backend()", 1)[1].split("-m uvicorn", 1)[0]

    assert "CORSIA_TEST_MODE=1" in launch


def test_worktree_test_mode_skips_production_release_pairing(monkeypatch) -> None:
    """The supported isolated backend path does not inspect production launchd."""
    events: list[str] = []
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv("CORSIA_SERVICE_GENERATION", raising=False)
    monkeypatch.setattr(
        frontend_build,
        "ensure_frontend_built",
        lambda: pytest.fail("test mode reached production release pairing"),
    )
    monkeypatch.setattr(
        "infra.file_descriptors.raise_file_descriptor_limit",
        lambda: events.append("file-limit"),
    )

    startup_prerequisites.prepare_startup_prerequisites()

    assert events == ["file-limit"]


def test_installed_service_cannot_use_test_mode_to_skip_pairing(monkeypatch) -> None:
    """A service-generation fact makes production pairing load-bearing."""
    events: list[str] = []
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.setenv("CORSIA_SERVICE_GENERATION", "installed-generation")
    monkeypatch.setattr(
        "infra.file_descriptors.raise_file_descriptor_limit",
        lambda: events.append("file-limit"),
    )
    monkeypatch.setattr(
        frontend_build,
        "ensure_frontend_built",
        lambda: events.append("frontend"),
    )

    startup_prerequisites.prepare_startup_prerequisites()

    assert events == ["file-limit", "frontend"]


def test_descriptor_limit_precedes_frontend_pairing(monkeypatch) -> None:
    """The frontend child inherits the process's raised descriptor ceiling."""
    events: list[str] = []
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    monkeypatch.delenv("CORSIA_SERVICE_GENERATION", raising=False)
    monkeypatch.setattr(
        "infra.file_descriptors.raise_file_descriptor_limit",
        lambda: events.append("file-limit"),
    )
    monkeypatch.setattr(
        frontend_build,
        "ensure_frontend_built",
        lambda: events.append("frontend"),
    )

    startup_prerequisites.prepare_startup_prerequisites()

    assert events == ["file-limit", "frontend"]


def test_packaged_desktop_skips_checkout_release_pairing(monkeypatch, tmp_path) -> None:
    """An immutable app bundle serves its included frontend without checkout scripts."""
    monkeypatch.setattr(frontend_build, "app_bundle_root", lambda: tmp_path / "CORSIA.app")
    monkeypatch.setattr(frontend_build, "FRONTEND_ROOT", tmp_path)
    monkeypatch.setattr(
        frontend_build.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("desktop bundle ran checkout release pairing"),
    )

    frontend_build.ensure_frontend_built()


def test_an_environment_variable_cannot_claim_app_mode(monkeypatch, tmp_path) -> None:
    """App mode is the module's location; a source install still pairs its release."""
    run = Mock(return_value=subprocess.CompletedProcess(["corsia"], 0))
    monkeypatch.setenv("CORSIA_DESKTOP_BUNDLE", "1")
    monkeypatch.setattr(frontend_build, "FRONTEND_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build.subprocess, "run", run)

    frontend_build.ensure_frontend_built()

    assert run.call_count == 1


def test_frontend_pairing_passes_resolved_runtime_identity(monkeypatch, tmp_path) -> None:
    """Direct Uvicorn startup cannot re-enter interactive CLI initialization."""
    script = tmp_path / "scripts" / "corsia"
    run = Mock(return_value=subprocess.CompletedProcess([str(script)], 0))
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    monkeypatch.setattr(frontend_build, "FRONTEND_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build, "_SERVING_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build, "_CORSIA_SCRIPT", script)
    monkeypatch.setattr(frontend_build, "WORKSPACE", workspace, raising=False)
    monkeypatch.setattr(frontend_build, "STATE_ROOT", state_root, raising=False)
    monkeypatch.setattr(frontend_build.subprocess, "run", run)
    monkeypatch.delenv("CORSIA_WORKSPACE", raising=False)
    monkeypatch.delenv("CORSIA_STATE_DIR", raising=False)

    frontend_build.ensure_frontend_built()

    env = run.call_args.kwargs["env"]
    assert env["CORSIA_WORKSPACE"] == str(workspace)
    assert env["CORSIA_STATE_DIR"] == str(state_root)


def test_frontend_pairing_delegates_to_exact_checkout_owner(monkeypatch, tmp_path) -> None:
    """The Python boundary supplies one exact checkout, interpreter, and command."""
    script = tmp_path / "scripts" / "corsia"
    run = Mock(return_value=subprocess.CompletedProcess([str(script)], 0))
    monkeypatch.setattr(frontend_build, "FRONTEND_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build, "_SERVING_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build, "_CORSIA_SCRIPT", script)
    monkeypatch.setattr(frontend_build, "WORKSPACE", Path("/isolated/workspace"))
    monkeypatch.setattr(frontend_build, "STATE_ROOT", Path("/isolated/state"))
    monkeypatch.setattr(frontend_build.subprocess, "run", run)

    frontend_build.ensure_frontend_built()

    run.assert_called_once()
    command = run.call_args.args[0]
    options = run.call_args.kwargs
    assert command == [str(script), "ensure-current-release"]
    assert options["cwd"] == tmp_path
    assert options["env"]["CORSIA_DIR"] == str(tmp_path)
    assert options["env"]["CORSIA_PYTHON"]
    assert options["env"]["CORSIA_WORKSPACE"] == "/isolated/workspace"
    assert options["env"]["CORSIA_STATE_DIR"] == "/isolated/state"
    assert options["check"] is False


def test_failed_release_pairing_command_is_fatal(monkeypatch, tmp_path) -> None:
    """A failed build/activation owner must abort lifespan instead of binding."""
    monkeypatch.setattr(frontend_build, "FRONTEND_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build, "_SERVING_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(
        frontend_build,
        "_CORSIA_SCRIPT",
        tmp_path / "scripts" / "corsia",
        raising=False,
    )
    monkeypatch.setattr(frontend_build, "_is_stale", lambda: True, raising=False)
    monkeypatch.setattr(
        frontend_build.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7),
    )

    with pytest.raises(RuntimeError, match="frontend release pairing failed"):
        frontend_build.ensure_frontend_built()
