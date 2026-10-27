
from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from service_script_fixtures import fake_checkout
from infra.default_port import DEFAULT_BACKEND_PORT

REPO_ROOT = Path(__file__).resolve().parents[2]
CORSIA_SCRIPT = REPO_ROOT / "scripts" / "corsia"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def isolated_unloaded_service_path(tmp_path: Path) -> str:
    """Return a PATH whose launchd probe cannot observe the host service."""
    fake_bin = tmp_path / "isolated-service-bin"
    fake_bin.mkdir(exist_ok=True)
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 1\n")
    fake_launchctl.chmod(0o755)
    return f"{fake_bin}:{SYSTEM_PATH}"


def install_service(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    env_file_text: str = "",
    label: str = "com.corsia.prod",
) -> tuple[Path, dict, subprocess.CompletedProcess]:
    checkout = fake_checkout(tmp_path / "corsia-home")
    agents_dir = tmp_path / "agents"
    env_file = tmp_path / "service.env"
    env_file.write_text(env_file_text)
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": isolated_unloaded_service_path(tmp_path),
        "BACKEND_PORT": "3999",
        "LAUNCH_AGENTS_DIR": str(agents_dir),
        "CORSIA_ENV_FILE": str(env_file),
        "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
        "CORSIA_STATE_DIR": str(tmp_path / "state"),
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"install failed:\n{proc.stdout}\n{proc.stderr}"
    plist_path = agents_dir / f"{label}.plist"
    assert plist_path.exists(), f"plist not written: {plist_path}"
    with open(plist_path, "rb") as f:
        return plist_path, plistlib.load(f), proc


def test_service_plist_pins_python_port_workdir(tmp_path):
    _, plist, _ = install_service(tmp_path)
    corsia_dir = tmp_path / "corsia-home"

    assert plist["Label"] == "com.corsia.prod"
    args = plist["ProgramArguments"]
    assert args[0] == str(corsia_dir / ".venv" / "bin" / "python")
    assert args[1:4] == ["-m", "uvicorn", "main:app"]
    assert args[args.index("--host") + 1] == "0.0.0.0"
    assert args[args.index("--port") + 1] == "3999"
    assert plist["WorkingDirectory"] == str(corsia_dir / "backend")
    assert plist["EnvironmentVariables"]["BACKEND_PORT"] == "3999"
    assert "CORSIA_ENV" not in plist["EnvironmentVariables"]
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    # The log lives with this instance's state so two checkouts on one machine
    # never write to, truncate, or tail one shared file.
    assert plist["StandardOutPath"] == str(tmp_path / "state" / "backend.log")
    assert plist["StandardErrorPath"] == str(tmp_path / "state" / "backend.log")


def test_script_operates_only_the_checkout_it_lives_in(tmp_path):
    """The installed service describes the script's checkout, never a default.

    ``scripts/corsia`` used to default CORSIA_DIR to ``~/.corsia``, so a
    worktree copy with the variable unset silently rendered, started, and
    restarted production. The checkout containing the script is the instance.
    """
    _, plist, _ = install_service(tmp_path)
    checkout = tmp_path / "corsia-home"

    assert plist["WorkingDirectory"] == str(checkout / "backend")
    assert plist["ProgramArguments"][0] == str(checkout / ".venv" / "bin" / "python")


def test_env_file_cannot_promote_a_worktree_to_the_canonical_checkout(tmp_path):
    """Checkout identity is fixed before the checkout's env file is sourced."""
    original_home = tmp_path / "original-home"
    spoof_home = tmp_path / "spoof-home"
    checkout = fake_checkout(spoof_home / ".corsia")
    env_file = tmp_path / "service.env"
    env_file.write_text(
        f"HOME={spoof_home.resolve()}\nCHECKOUT_DIR={checkout.resolve()}\n"
    )
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(original_home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "CORSIA_ENV_FILE": str(env_file),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "LAUNCH_AGENTS_DIR": str(agents_dir),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert f"requires an explicit BACKEND_PORT other than {DEFAULT_BACKEND_PORT}" in proc.stderr
    assert not agents_dir.exists()


def test_script_refuses_a_corsia_dir_naming_another_checkout(tmp_path):
    """An explicit request to operate a different checkout is refused, not redirected."""
    checkout = fake_checkout(tmp_path / "checkout")
    other = tmp_path / "other-checkout"
    other.mkdir()
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "CORSIA_DIR": str(other),
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "is not this script's checkout" in proc.stderr
    assert not agents_dir.exists()


def test_noncanonical_mutation_refuses_machine_bootstrap_roots(tmp_path):
    """A worktree must not inherit production workspace or state implicitly.

    The fake interpreter models a successful import of the machine-wide
    bootstrap config. Before the guard, service install accepted those roots
    and rendered them into this checkout's plist.
    """
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *CORSIA_RUNTIME_IDENTITY*) "
        "printf '__CORSIA_RUNTIME_IDENTITY__\\t/prod/workspace\\t/prod/state\\n' ;;\n"
        "  *) printf 'operator\\n' ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    checkout = fake_checkout(tmp_path / "checkout", python=fake_python)
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "requires explicit CORSIA_WORKSPACE and CORSIA_STATE_DIR" in proc.stderr
    assert not agents_dir.exists()


def test_noncanonical_start_refuses_before_machine_bootstrap_setup(tmp_path):
    """A worktree may not initialize machine-wide identity before its guard."""
    trace = tmp_path / "python.log"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$TRACE\"\n"
        "exit 0\n"
    )
    fake_python.chmod(0o755)
    checkout = fake_checkout(tmp_path / "checkout", python=fake_python)

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "start"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "CORSIA_PYTHON": str(fake_python),
            "TRACE": str(trace),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "requires explicit CORSIA_WORKSPACE and CORSIA_STATE_DIR" in proc.stderr
    assert "first-time setup is only available from" in proc.stderr.lower()
    assert "scripts/corsia start" in proc.stderr
    assert not trace.exists()


def test_noncanonical_mutation_refuses_the_production_port_default(tmp_path):
    """Explicit isolated roots are insufficient when the port still means prod."""
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert f"requires an explicit BACKEND_PORT other than {DEFAULT_BACKEND_PORT}" in proc.stderr
    assert not agents_dir.exists()


@pytest.mark.parametrize("port", [f"0{DEFAULT_BACKEND_PORT}", "notaport", "0", "65536"])
def test_noncanonical_mutation_rejects_invalid_or_production_port_alias(
    tmp_path,
    port,
):
    """Port identity is numeric and normalized before the production check."""
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "BACKEND_PORT": port,
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert not agents_dir.exists()
    if port == f"0{DEFAULT_BACKEND_PORT}":
        assert f"other than {DEFAULT_BACKEND_PORT}" in proc.stderr
    else:
        assert "invalid backend_port" in proc.stderr.lower()


def test_noncanonical_mutation_normalizes_a_valid_explicit_port(tmp_path):
    """A valid zero-padded nonproduction port has one canonical identity."""
    _, plist, _ = install_service(
        tmp_path,
        extra_env={"BACKEND_PORT": "03999"},
    )

    assert plist["EnvironmentVariables"]["BACKEND_PORT"] == "3999"
    args = plist["ProgramArguments"]
    assert args[args.index("--port") + 1] == "3999"


def test_noncanonical_mutation_refuses_explicit_production_bootstrap_roots(tmp_path):
    """Naming production's roots explicitly does not make them isolated."""
    home = tmp_path / "home"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    workspace = tmp_path / "prod-workspace"
    state_root = tmp_path / "prod-state"
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-id",
                    "name": "workspace",
                    "root": str(workspace),
                },
                "state_root": str(state_root),
            }
        )
    )
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(workspace),
            "CORSIA_STATE_DIR": str(state_root),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "production bootstrap" in proc.stderr.lower()
    assert not agents_dir.exists()


def test_noncanonical_mutation_accepts_distinct_roots_with_production_bootstrap(
    tmp_path,
):
    """Existing sibling roots remain valid when neither overlaps production."""
    home = tmp_path / "home"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    prod_workspace = tmp_path / "prod-workspace"
    prod_state = tmp_path / "prod-state"
    selected_workspace = tmp_path / "isolated-workspace"
    selected_state = tmp_path / "isolated-state"
    for root in (prod_workspace, prod_state, selected_workspace, selected_state):
        root.mkdir()
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-id",
                    "name": "workspace",
                    "root": str(prod_workspace),
                },
                "state_root": str(prod_state),
            }
        )
    )
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(selected_workspace),
            "CORSIA_STATE_DIR": str(selected_state),
        },
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    with (agents_dir / "com.corsia.prod.plist").open("rb") as plist_file:
        identity = plistlib.load(plist_file)["EnvironmentVariables"]
    assert identity["CORSIA_WORKSPACE"] == str(selected_workspace.resolve())
    assert identity["CORSIA_STATE_DIR"] == str(selected_state.resolve())


@pytest.mark.parametrize(
    ("selected_workspace", "selected_state"),
    [
        ("prod-state", "isolated-state"),
        ("isolated-workspace", "prod-workspace"),
    ],
)
def test_noncanonical_mutation_refuses_crossed_production_roots(
    tmp_path,
    selected_workspace,
    selected_state,
):
    """Neither selected root may overlap either production runtime root."""
    home = tmp_path / "home"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    prod_workspace = tmp_path / "prod-workspace"
    prod_state = tmp_path / "prod-state"
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-id",
                    "name": "workspace",
                    "root": str(prod_workspace),
                },
                "state_root": str(prod_state),
            }
        )
    )
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(tmp_path / selected_workspace),
            "CORSIA_STATE_DIR": str(tmp_path / selected_state),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "production bootstrap" in proc.stderr.lower()
    assert not agents_dir.exists()


@pytest.mark.parametrize("relative", [Path(), Path("future")], ids=["root", "missing-child"])
def test_noncanonical_mutation_refuses_case_alias_of_production_root(
    tmp_path,
    relative,
):
    """A case alias of production, including a missing child, is not isolated."""
    if sys.platform != "darwin":
        pytest.skip("case-insensitive production-root alias is a macOS contract")

    home = tmp_path / "home"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    prod_workspace = tmp_path / "ProdWorkspace"
    prod_state = tmp_path / "ProdState"
    prod_workspace.mkdir()
    prod_state.mkdir()
    workspace_alias = prod_workspace.with_name(prod_workspace.name.swapcase())
    if not workspace_alias.exists() or not workspace_alias.samefile(prod_workspace):
        pytest.skip("test volume is case-sensitive")
    assert workspace_alias != prod_workspace
    selected_workspace = workspace_alias / relative
    if relative != Path():
        assert not selected_workspace.exists()

    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-id",
                    "name": "workspace",
                    "root": str(prod_workspace),
                },
                "state_root": str(prod_state),
            }
        )
    )
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(selected_workspace),
            "CORSIA_STATE_DIR": str(tmp_path / "isolated-state"),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "production bootstrap" in proc.stderr.lower()
    assert not agents_dir.exists()


def test_noncanonical_mutation_refuses_existing_child_through_macos_firmlink(
    tmp_path,
):
    """An existing child reached through a firmlink still overlaps production."""
    if sys.platform != "darwin":
        pytest.skip("firmlink production-root alias is a macOS contract")

    firmlink_checkout = Path("/System/Volumes/Data") / REPO_ROOT.relative_to("/")
    selected_workspace = firmlink_checkout / "backend"
    if (
        not selected_workspace.exists()
        or not firmlink_checkout.samefile(REPO_ROOT)
    ):
        pytest.skip("test checkout is not available through the macOS data firmlink")
    assert selected_workspace.parent.samefile(REPO_ROOT)

    home = tmp_path / "home"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    prod_state = tmp_path / "prod-state"
    selected_state = tmp_path / "isolated-state"
    prod_state.mkdir()
    selected_state.mkdir()
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-id",
                    "name": "workspace",
                    "root": str(REPO_ROOT),
                },
                "state_root": str(prod_state),
            }
        )
    )
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_WORKSPACE": str(selected_workspace),
            "CORSIA_STATE_DIR": str(selected_state),
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "production bootstrap" in proc.stderr.lower()
    assert not agents_dir.exists()


def test_noncanonical_discard_prepared_is_guarded_before_deletion(tmp_path):
    """Discarding staged assets is a lifecycle mutation, not a read-only verb."""
    checkout = fake_checkout(tmp_path / "checkout")
    stage = checkout / "frontend" / "node_modules" / ".corsia-dist-stage"
    stage.mkdir(parents=True)
    (stage / "asset.js").write_text("prepared")
    prepared = stage.with_name(f"{stage.name}.prepared")
    prepared.write_text("")

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "discard-prepared"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": SYSTEM_PATH,
            "CORSIA_RESTART_CALLER_TOKEN": "test-caller",
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert stage.exists()
    assert prepared.exists()
    assert "requires explicit corsia_workspace" in proc.stderr.lower()


def test_noncanonical_logs_remain_read_only(tmp_path):
    """Reading this checkout's resolved log is not reported as a mutation."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-id",
                    "name": "workspace",
                    "root": str(workspace),
                },
                "state_root": str(state_root),
            }
        )
    )
    checkout = fake_checkout(tmp_path / "checkout")
    trace = tmp_path / "tail.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tail = fake_bin / "tail"
    fake_tail.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$TRACE\"\n")
    fake_tail.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "logs"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:{SYSTEM_PATH}",
            "TRACE": str(trace),
            "CORSIA_RESTART_CALLER_TOKEN": "test-caller",
        },
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert trace.read_text().strip() == f"-f {state_root}/backend.log"
    assert "lifecycle mutation" not in proc.stderr.lower()


def test_service_label_comes_from_this_checkouts_env(tmp_path):
    """A second checkout names its own job; the default stays com.corsia.prod."""
    plist_path, plist, _ = install_service(
        tmp_path,
        env_file_text="CORSIA_SERVICE_LABEL=com.corsia.dev-2\n",
        label="com.corsia.dev-2",
    )

    assert plist_path.name == "com.corsia.dev-2.plist"
    assert plist["Label"] == "com.corsia.dev-2"
    assert not (plist_path.parent / "com.corsia.prod.plist").exists()


def test_invalid_service_label_is_refused_before_any_effect(tmp_path):
    checkout = fake_checkout(tmp_path / "checkout")
    agents_dir = tmp_path / "agents"
    env_file = tmp_path / "service.env"
    env_file.write_text("CORSIA_SERVICE_LABEL=../escape\n")

    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "CORSIA_ENV_FILE": str(env_file),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "BACKEND_PORT": "3999",
        },
        timeout=60,
    )

    assert proc.returncode != 0
    assert "is not a launchd label" in proc.stdout + proc.stderr
    assert not agents_dir.exists()


@pytest.mark.parametrize(
    ("arguments", "expect_bootstrap"),
    [
        (("start",), True),
        # --force is the one documented argument and must reach the operation.
        (("start", "--force"), True),
        # The bare verb is the whole story now. A service selector never
        # selected anything else, so it must be refused rather than silently
        # accepted -- and refusal must not still start the service.
        (("start", "server"), False),
        # An unknown option is refused for the same reason: accepting it
        # would perform the operation for a command the user did not type.
        (("start", "--bogus"), False),
    ],
)
def test_corsia_start_bootstraps_an_installed_unloaded_service(
    tmp_path, arguments, expect_bootstrap,
):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    plist_path = agents_dir / "com.corsia.prod.plist"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = tmp_path / "fake-python"
    corsia_dir = fake_checkout(tmp_path / "corsia-home", python=fake_python)
    # A `corsia` symlink in PATH resolves to the checkout it points at.
    installed_corsia = fake_bin / "corsia"
    installed_corsia.symlink_to(corsia_dir / "scripts" / "corsia")
    launchctl_state = tmp_path / "launchctl-state"
    launchctl_capture = tmp_path / "launchctl-capture"

    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(
        """#!/bin/sh
case "$1" in
    print) [ -f "$LAUNCHCTL_STATE" ] ;;
    bootstrap)
        printf '%s\\n' "$*" > "$LAUNCHCTL_CAPTURE"
        : > "$LAUNCHCTL_STATE"
        ;;
    *) exit 1 ;;
esac
"""
    )
    fake_launchctl.chmod(0o755)

    fake_lsof = fake_bin / "lsof"
    fake_lsof.write_text("#!/bin/sh\nexit 1\n")
    fake_lsof.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/bin/sh
case "$*" in
    *api/health*) [ -f "$LAUNCHCTL_STATE" ] ;;
    *api/version*)
        printf '%s' '{"backend":{"full_hash":"abcdef1234567890abcdef1234567890abcdef12","current_head":{"full_hash":"abcdef1234567890abcdef1234567890abcdef12"}},"frontend":{"full_hash":"abcdef1234567890abcdef1234567890abcdef12"}}'
        ;;
    *) exit 1 ;;
esac
"""
    )
    fake_curl.chmod(0o755)

    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "    *load_bootstrap*) printf 'isolated\\n' ;;\n"
        "    *\"STATE_ROOT, WORKSPACE\"*)\n"
        "        printf '__CORSIA_RUNTIME_IDENTITY__\\t%s\\t%s\\n' \"$CORSIA_WORKSPACE\" \"$CORSIA_STATE_DIR\"\n"
        "        ;;\n"
        "    *) printf 'test-token\\n' ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    with plist_path.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": [str(fake_python)],
                "WorkingDirectory": str(corsia_dir / "backend"),
                "EnvironmentVariables": {
                    "BACKEND_PORT": "3999",
                    "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
                    "CORSIA_STATE_DIR": str(tmp_path / "state"),
                },
            },
            plist_file,
        )

    proc = subprocess.run(
        ["bash", str(installed_corsia), *arguments],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "CORSIA_PYTHON": str(fake_python),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "LAUNCHCTL_STATE": str(launchctl_state),
            "LAUNCHCTL_CAPTURE": str(launchctl_capture),
        },
        timeout=10,
    )

    if not expect_bootstrap:
        assert proc.returncode != 0, (proc.stdout, proc.stderr)
        assert not launchctl_capture.exists(), "a refused selector still started the service"
        assert "Unexpected argument" in proc.stdout + proc.stderr
        return

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert launchctl_capture.read_text().startswith("bootstrap ")
    assert "com.corsia.prod.plist" in launchctl_capture.read_text()
    assert "Health: ok" in proc.stdout
    assert "Process: launchd com.corsia.prod" in proc.stdout


def test_inherited_corsia_env_is_not_rendered_into_the_plist(tmp_path):
    """CORSIA runs one instance; no env selector may re-enter the service."""
    _, plist, _ = install_service(tmp_path, extra_env={"CORSIA_ENV": "dev"})
    assert "CORSIA_ENV" not in plist["EnvironmentVariables"]


def test_restart_handoff_variables_are_not_rendered_into_the_plist(tmp_path):
    _, plist, _ = install_service(
        tmp_path,
        env_file_text=(
            "CORSIA_RESTART_BACKEND_PORT=3002\n"
            "CORSIA_RESTART_OPERATION_ID=stale-operation\n"
        ),
    )

    environment = plist["EnvironmentVariables"]
    assert "CORSIA_RESTART_BACKEND_PORT" not in environment
    assert "CORSIA_RESTART_OPERATION_ID" not in environment


def test_test_only_safety_bypasses_are_not_rendered_into_the_plist(tmp_path):
    _, plist, proc = install_service(
        tmp_path,
        env_file_text=(
            "CORSIA_TEST_MODE=1\n"
            "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1\n"
            "CORSIA_AGENT_RESTART_ALLOWED=1\n"
            "CORSIA_ANALYTICS_DISABLE=1\n"
            "VISIBLE_SETTING=preserved\n"
        ),
    )

    environment = plist["EnvironmentVariables"]
    assert "CORSIA_TEST_MODE" not in environment
    assert "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI" not in environment
    assert environment["CORSIA_AGENT_RESTART_ALLOWED"] == "1"
    assert environment["CORSIA_ANALYTICS_DISABLE"] == "1"
    assert environment["VISIBLE_SETTING"] == "preserved"
    combined = proc.stdout + proc.stderr
    assert "sets CORSIA_TEST_MODE — ignored" in combined
    assert "sets CORSIA_ALLOW_HOME_WORKSPACE_IN_CI — ignored" in combined


def test_env_file_cannot_override_service_generation(tmp_path):
    _, plist, proc = install_service(
        tmp_path,
        env_file_text="CORSIA_SERVICE_GENERATION=injected-generation\n",
    )

    generation = plist["EnvironmentVariables"]["CORSIA_SERVICE_GENERATION"]
    assert generation != "injected-generation"
    assert generation
    assert "sets CORSIA_SERVICE_GENERATION — ignored" in proc.stdout + proc.stderr


def test_service_identity_roots_round_trip_through_the_rendered_plist(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    _, plist, proc = install_service(
        tmp_path,
        extra_env={"CORSIA_WORKSPACE": str(workspace)},
        env_file_text='CORSIA_STATE_DIR="$HOME/state"\n',
    )

    identity = plist["EnvironmentVariables"]
    assert identity["CORSIA_WORKSPACE"] == str(workspace.resolve())
    assert identity["CORSIA_STATE_DIR"] == str((home / "state").resolve())
    combined = proc.stdout + proc.stderr
    assert "CORSIA_STATE_DIR is resolved by infra.config" in combined
    assert "CORSIA_STATE_DIR — ignored" not in combined


def test_zero_configuration_install_pins_bootstrapped_workspace_and_state(tmp_path):
    home = tmp_path / "home"
    corsia_dir = fake_checkout(home / ".corsia")
    agents_dir = tmp_path / "agents"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text(
        json.dumps({
            "version": 2,
            "workspace": {
                "id": "workspace-id",
                "name": "workspace",
                "root": str(workspace),
            },
            "state_root": str(state_root),
        })
    )

    proc = subprocess.run(
        ["bash", str(corsia_dir / "scripts" / "corsia"), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "LAUNCH_AGENTS_DIR": str(agents_dir),
        },
        timeout=60,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    with (agents_dir / "com.corsia.prod.plist").open("rb") as plist_file:
        identity = plistlib.load(plist_file)["EnvironmentVariables"]
    assert identity["CORSIA_WORKSPACE"] == str(workspace.resolve())
    assert identity["CORSIA_STATE_DIR"] == str(state_root.resolve())


def test_request_bound_port_cannot_be_overridden_by_the_env_file(tmp_path):
    env_file = tmp_path / "service.env"
    env_file.write_text(
        f"BACKEND_PORT={DEFAULT_BACKEND_PORT}\n"
        "CORSIA_RESTART_BACKEND_PORT=3002\n"
        "CORSIA_RESTART_OPERATION_ID=stale-operation\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$CORSIA_RESTART_OPERATION_ID\" >> \"$CAPTURE\"\n"
        "exit 1\n"
    )
    fake_curl.chmod(0o755)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "    *load_bootstrap*) printf 'isolated\\n' ;;\n"
        "    *\"STATE_ROOT, WORKSPACE\"*)\n"
        "        printf '__CORSIA_RUNTIME_IDENTITY__\\t%s\\t%s\\n' \"$CORSIA_WORKSPACE\" \"$CORSIA_STATE_DIR\"\n"
        "        ;;\n"
        "    *) printf 'test-token\\n' ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    capture = tmp_path / "curl-env.log"

    proc = subprocess.run(
        ["bash", str(CORSIA_SCRIPT), "status"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "CORSIA_ENV_FILE": str(env_file),
            "CORSIA_PYTHON": str(fake_python),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "CORSIA_RESTART_BACKEND_PORT": "3001",
            "CORSIA_RESTART_OPERATION_ID": "request-operation",
            "CAPTURE": str(capture),
        },
        timeout=10,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "http://localhost:3001" in proc.stdout
    assert f"http://localhost:{DEFAULT_BACKEND_PORT}" not in proc.stdout
    assert set(capture.read_text().splitlines()) == {"request-operation"}


def test_status_does_not_require_runtime_identity_resolution(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 1\n")
    fake_python.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(CORSIA_SCRIPT), "status"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "CORSIA_PYTHON": str(fake_python),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "BACKEND_PORT": "3999",
        },
        timeout=10,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "CORSIA Status" in proc.stdout
    assert "Could not resolve CORSIA workspace and state identity" not in proc.stderr


def test_logs_do_not_require_runtime_identity_resolution(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexit 1\n")
    fake_python.chmod(0o755)
    fake_tail = fake_bin / "tail"
    fake_tail.write_text("#!/bin/sh\nprintf 'logs-ok\\n'\n")
    fake_tail.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(CORSIA_SCRIPT), "logs"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "CORSIA_PYTHON": str(fake_python),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "BACKEND_LOG": str(tmp_path / "backend.log"),
        },
        timeout=10,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stdout == "logs-ok\n"
    assert "Could not resolve CORSIA workspace and state identity" not in proc.stderr


def test_runtime_identity_ignores_import_stdout_before_the_sentinel(tmp_path):
    agents_dir = tmp_path / "agents"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "    *load_bootstrap*) printf 'isolated\\n' ;;\n"
        "    *\"STATE_ROOT, WORKSPACE\"*)\n"
        "        printf 'import noise\\n__CORSIA_RUNTIME_IDENTITY__\\t%s\\t%s\\n' "
        "\"$CORSIA_WORKSPACE\" \"$CORSIA_STATE_DIR\"\n"
        "        ;;\n"
        "    *) printf 'test-token\\n' ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(CORSIA_SCRIPT), "service", "install"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": isolated_unloaded_service_path(tmp_path),
            "CORSIA_PYTHON": str(fake_python),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "BACKEND_PORT": "3999",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
        },
        timeout=10,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    with (agents_dir / "com.corsia.prod.plist").open("rb") as plist_file:
        identity = plistlib.load(plist_file)["EnvironmentVariables"]
    assert identity["CORSIA_WORKSPACE"] == str(tmp_path / "workspace")
    assert identity["CORSIA_STATE_DIR"] == str(tmp_path / "state")


def test_restart_installer_upgrades_owned_legacy_plist_without_root_fields(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    backend_dir = tmp_path / "serving" / "backend"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    plist_path = agents_dir / "com.corsia.prod.plist"
    with plist_path.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": [sys.executable],
                "WorkingDirectory": str(backend_dir),
                "EnvironmentVariables": {
                    "BACKEND_PORT": "3999",
                    "CORSIA_SERVICE_GENERATION": "legacy-generation",
                },
            },
            plist_file,
        )

    proc = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
resolve_bind_host() { echo 0.0.0.0; }
service_is_loaded() { return 0; }
service_loaded_definition_matches() { return 0; }
service_install restart
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "BACKEND_DIR": str(backend_dir),
            "BACKEND_PORT": "3999",
            "BACKEND_LOG": str(tmp_path / "backend.log"),
            "PYTHON": sys.executable,
            "ENV_FILE": str(tmp_path / "missing.env"),
            "CORSIA_WORKSPACE": str(workspace),
            "CORSIA_STATE_DIR": str(state_root),
        },
        timeout=10,
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    with plist_path.open("rb") as plist_file:
        identity = plistlib.load(plist_file)["EnvironmentVariables"]
    assert identity["CORSIA_WORKSPACE"] == str(workspace)
    assert identity["CORSIA_STATE_DIR"] == str(state_root)


def test_restart_refuses_a_partially_pinned_legacy_plist(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    backend_dir = tmp_path / "serving" / "backend"
    plist_path = agents_dir / "com.corsia.prod.plist"
    with plist_path.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": [sys.executable],
                "WorkingDirectory": str(backend_dir),
                "EnvironmentVariables": {
                    "BACKEND_PORT": "3999",
                    "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
                },
            },
            plist_file,
        )
    original = plist_path.read_bytes()

    proc = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_loaded_definition_matches() { return 0; }
service_install restart
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "BACKEND_DIR": str(backend_dir),
            "BACKEND_PORT": "3999",
            "BACKEND_LOG": str(tmp_path / "backend.log"),
            "PYTHON": sys.executable,
            "ENV_FILE": str(tmp_path / "missing.env"),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
        },
        timeout=10,
    )

    assert proc.returncode != 0
    assert "partially pins CORSIA runtime roots" in proc.stderr
    assert plist_path.read_bytes() == original


def test_stop_loaded_job_without_plist_gives_manual_recovery_guidance(tmp_path):
    result = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
FORCE=true
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_stop
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(tmp_path / "agents"),
            "BACKEND_PORT": "3999",
            "PYTHON": sys.executable,
        },
        timeout=10,
    )

    assert result.returncode != 0
    assert "has no installed definition" in result.stderr
    assert "launchctl print" in result.stderr
    assert "corsia service install" not in result.stderr


def test_start_loaded_job_without_plist_gives_manual_recovery_guidance(tmp_path):
    result = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_start
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(tmp_path / "agents"),
            "BACKEND_PORT": "3999",
            "PYTHON": sys.executable,
        },
        timeout=10,
    )

    assert result.returncode != 0
    assert "has no installed definition" in result.stderr
    assert "launchctl print" in result.stderr
    assert "corsia service install" not in result.stderr


def test_install_refuses_an_unloaded_foreign_definition_before_replacing_it(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    expected_backend = tmp_path / "serving" / "backend"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    plist_path = agents_dir / "com.corsia.prod.plist"
    with plist_path.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": [sys.executable],
                "WorkingDirectory": str(tmp_path / "foreign" / "backend"),
                "EnvironmentVariables": {
                    "BACKEND_PORT": "3999",
                    "CORSIA_WORKSPACE": str(workspace),
                    "CORSIA_STATE_DIR": str(state_root),
                },
            },
            plist_file,
        )
    original = plist_path.read_bytes()
    trace = tmp_path / "effects.log"

    proc = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
launchctl() {
    [ "$1" != "print" ] || return 1
    printf 'launchctl %s\\n' "$*" >> "$TRACE"
}
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_install
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "BACKEND_DIR": str(expected_backend),
            "BACKEND_PORT": "3999",
            "PYTHON": sys.executable,
            "CORSIA_WORKSPACE": str(workspace),
            "CORSIA_STATE_DIR": str(state_root),
            "TRACE": str(trace),
        },
        timeout=10,
    )

    assert proc.returncode != 0
    assert "does not describe this checkout" in proc.stderr
    assert "Refusing to replace unowned definition" in proc.stderr
    assert plist_path.read_bytes() == original
    assert not trace.exists()


def test_restart_treats_another_checkouts_definition_as_no_service_of_its_own(tmp_path):
    """A plist under this label for another checkout is not this checkout's job.

    Restart takes the direct-process route for its own port and never touches
    launchd, so the other checkout's service is neither replaced nor booted out.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    plist_path = agents_dir / "com.corsia.prod.plist"
    with plist_path.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "WorkingDirectory": str(tmp_path / "foreign" / "backend"),
            },
            plist_file,
        )
    original = plist_path.read_bytes()
    trace = tmp_path / "effects.log"
    expected_backend = tmp_path / "serving" / "backend"

    proc = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
FORCE=true
BACKEND_PORT=3999
CORSIA_TOKEN=test-token
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
resolve_bind_host() { echo 0.0.0.0; }
launchctl() {
    [ "$1" != "print" ] || return 1
    printf 'launchctl %s\\n' "$*" >> "$TRACE"
}
is_backend_running() { return 1; }
backend_pids() { :; }
start_backend() { printf 'legacy start\\n' >> "$TRACE"; }
wait_for_backend_health() { :; }
prove_backend_replaced() { :; }
print_backend_status() { :; }
sleep() { :; }
bounce_server
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "BACKEND_DIR": str(expected_backend),
            "TRACE": str(trace),
        },
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    assert trace.read_text().splitlines() == ["legacy start"]
    assert plist_path.read_bytes() == original


def test_restart_refuses_a_service_definition_for_another_state_root(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    expected_backend = tmp_path / "serving" / "backend"
    expected_workspace = tmp_path / "workspace"
    expected_state = tmp_path / "state"
    plist_path = agents_dir / "com.corsia.prod.plist"
    with plist_path.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": ["/usr/bin/python3"],
                "WorkingDirectory": str(expected_backend),
                "EnvironmentVariables": {
                    "BACKEND_PORT": "3999",
                    "CORSIA_WORKSPACE": str(expected_workspace),
                    "CORSIA_STATE_DIR": str(tmp_path / "foreign-state"),
                },
            },
            plist_file,
        )
    trace = tmp_path / "effects.log"

    proc = subprocess.run(
        ["/bin/bash", "-c", """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=3999
PYTHON=/usr/bin/python3
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
is_backend_running() { return 1; }
launchctl() {
    [ "$1" != "print" ] || return 1
    printf 'launchctl %s\\n' "$*" >> "$TRACE"
}
bounce_server
"""],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LAUNCH_AGENTS_DIR": str(agents_dir),
            "BACKEND_DIR": str(expected_backend),
            "CORSIA_WORKSPACE": str(expected_workspace),
            "CORSIA_STATE_DIR": str(expected_state),
            "TRACE": str(trace),
        },
        timeout=10,
    )

    assert proc.returncode != 0
    assert "different CORSIA_STATE_DIR" in proc.stderr
    assert not trace.exists()


def test_env_file_keys_rendered_and_pinned_keys_refused(tmp_path):
    _, plist, proc = install_service(
        tmp_path,
        env_file_text=(
            "# comment line\n"
            'export TEST_API_KEY="abc&def"\n'
            "export CORSIA_E2E_MODEL=ignored-model\n"
        ),
    )
    env_vars = plist["EnvironmentVariables"]
    assert env_vars["TEST_API_KEY"] == "abc&def"  # plistlib decodes &amp;
    assert "CORSIA_E2E_MODEL" not in env_vars
    combined = proc.stdout + proc.stderr
    assert "CORSIA_E2E_MODEL" in combined and "ignored" in combined


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available")
def test_rendered_plist_passes_plutil_lint(tmp_path):
    plist_path, _, _ = install_service(tmp_path)
    proc = subprocess.run(
        ["plutil", "-lint", "-s", str(plist_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"plutil -lint failed: {proc.stderr}"


def _stateful_launchctl(tmp_path: Path) -> tuple[str, Path]:
    """A launchctl double whose loaded state is a flag file."""
    fake_bin = tmp_path / "stateful-bin"
    fake_bin.mkdir(exist_ok=True)
    loaded = tmp_path / "loaded.flag"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        f"flag='{loaded}'\n"
        "case \"$1\" in\n"
        "    print) [ -f \"$flag\" ] && { echo '    pid = 4242'; exit 0; }; exit 1 ;;\n"
        "    bootstrap) : > \"$flag\" ;;\n"
        "    bootout) rm -f \"$flag\" ;;\n"
        "esac\n"
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    return f"{fake_bin}:{SYSTEM_PATH}", loaded


def _service_cli(checkout: Path, tmp_path: Path, path: str, port: str, *args: str):
    env_file = tmp_path / "service.env"
    env_file.touch()
    return subprocess.run(
        ["bash", str(checkout / "scripts" / "corsia"), *args],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": path,
            "BACKEND_PORT": port,
            "LAUNCH_AGENTS_DIR": str(tmp_path / "agents"),
            "CORSIA_ENV_FILE": str(env_file),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
        },
        timeout=60,
    )


def test_port_mismatch_refusal_names_the_move_and_the_env_alternative(tmp_path):
    path, loaded = _stateful_launchctl(tmp_path)
    checkout = fake_checkout(tmp_path / "corsia-home")
    old, new = "3997", "3996"
    assert _service_cli(checkout, tmp_path, path, old, "service", "install").returncode == 0
    assert _service_cli(checkout, tmp_path, path, old, "service", "start").returncode == 0
    assert loaded.exists()

    for verb in (("restart",), ("service", "start"), ("service", "stop"), ("service", "install")):
        proc = _service_cli(checkout, tmp_path, path, new, *verb)
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, verb
        assert f"targets backend port {old}, not request-bound port {new}" in out, verb
        assert f"BACKEND_PORT={old} corsia service stop" in out, verb
        assert f"BACKEND_PORT={old} corsia service uninstall" in out, verb
        assert (
            f"BACKEND_PORT={new} corsia service install; BACKEND_PORT={new} corsia service start"
            in out
        ), verb
        assert f"set BACKEND_PORT={old} in {tmp_path / 'service.env'}" in out, verb
    assert loaded.exists()


def test_port_mismatch_guidance_leaves_the_port_unset_only_for_the_default():
    script = (
        "fail() { :; }\n"
        "source scripts/corsia.d/service.sh\n"
        "ENV_FILE=/x/.env DEFAULT_BACKEND_PORT=1956\n"
        'service_port_mismatch_guidance l "$1" "$2"\n'
    )

    def guidance(installed: str, requested: str) -> str:
        return subprocess.run(
            ["/bin/bash", "-c", script, "x", installed, requested],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        ).stdout

    default = guidance("3997", "1956")
    assert "with BACKEND_PORT and CORSIA_RESTART_BACKEND_PORT unset" in default
    assert "corsia service install; corsia service start" in default
    explicit = guidance("3997", "3996")
    assert "BACKEND_PORT=3996 corsia service install" in explicit
    assert "BACKEND_PORT and CORSIA_RESTART_BACKEND_PORT unset" not in explicit


def test_documented_port_move_rehearsal_ends_at_the_new_port_started(tmp_path):
    path, loaded = _stateful_launchctl(tmp_path)
    checkout = fake_checkout(tmp_path / "corsia-home")
    old, new = "3997", "3996"
    steps = [
        (old, ("service", "install")),
        (old, ("service", "start")),
        (old, ("service", "uninstall")),
        (new, ("service", "install")),
        (new, ("service", "start")),
    ]
    for port, verb in steps:
        if verb == ("service", "uninstall"):
            # `service stop` drains through the live backend (generation match
            # and a credential), which a launchctl double cannot express; the
            # job simply ends up unloaded, as after a completed stop.
            loaded.unlink()
        proc = _service_cli(checkout, tmp_path, path, port, *verb)
        assert proc.returncode == 0, (verb, proc.stdout, proc.stderr)
    with (tmp_path / "agents" / "com.corsia.prod.plist").open("rb") as f:
        assert plistlib.load(f)["EnvironmentVariables"]["BACKEND_PORT"] == new
    assert loaded.exists()
