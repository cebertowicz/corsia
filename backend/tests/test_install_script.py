"""Post-clone installer behavior, exercised only with fake local commands."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from infra.default_port import DEFAULT_BACKEND_PORT

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install.sh"


BREW = """#!/bin/bash
set -eu
echo "brew $*" >> "$STUB_LOG"
if [ "$1" = --prefix ]; then
    printf '%s\n' "$STUB_BREW_PREFIX"
    exit 0
fi
if [ "${STUB_BREW_FAIL:-0}" = 1 ]; then
    exit 1
fi
if [ "$1" = install ] && [ "$2" = eugene1g/safehouse/agent-safehouse ]; then
    cp "$STUB_SAFEHOUSE" "$STUB_BIN/safehouse"
    chmod +x "$STUB_BIN/safehouse"
fi
if [ "$1" = install ] && [ "$2" = node ]; then
    printf '#!/bin/bash\\necho v22.12.0\\n' > "$STUB_BIN/node"
    chmod +x "$STUB_BIN/node"
fi
if [ "$1" = install ] && [ "$2" = gh ]; then
    printf '#!/bin/bash\\necho gh version 2.60.0\\n' > "$STUB_BIN/gh"
    chmod +x "$STUB_BIN/gh"
fi
"""

# The browser CLI: `--version` reports STUB_BROWSER_VERSION; `install` is only logged.
BROWSER_CLI = (
    'if [ "${1:-}" = --version ]; then echo "agent-browser ${STUB_BROWSER_VERSION:-0.31.2}"; exit 0; fi; '
    'echo "agent-browser $*" >> "$STUB_LOG"; [ "${STUB_BROWSER_SETUP_FAIL:-0}" != 1 ]'
)


# npm: logs the call; a global install puts the browser CLI on the service PATH.
NPM = (
    'echo "npm $*" >> "$STUB_LOG"; '
    'if [ "${STUB_NPM_FAIL:-0}" = 1 ]; then echo "npm ERR! real failure text" >&2; exit 1; fi; '
    'if [ "$1 $2" = "install -g" ]; then cp "$STUB_BROWSER_SOURCE" "$STUB_BIN/agent-browser"; fi'
)

STUBS = {
    "uname": 'case "$1" in -s) echo Darwin ;; -m) echo arm64 ;; esac',
    "sw_vers": 'echo "${STUB_MACOS_VERSION:-15.1}"',
    "git": (
        'case "$*" in *--version*) echo "git version ${STUB_GIT_VERSION:-2.39.5} (Apple Git-154)" ;; '
        '*describe*) echo test-version ;; *rev-parse*) echo test-sha ;; esac'
    ),
    "curl": '[ "${STUB_RUNNING:-0}" = 1 ]',
    "node": 'echo v22.12.0',
    "safehouse": 'echo safehouse 0.11.1',
    "gh": 'echo gh version 2.60.0',
    "agent-browser": BROWSER_CLI,
    "npm": NPM,
    "python3.11": (
        # The version check runs for real, against the SQLite this stub claims.
        'if [ "$1" = -c ]; then exec "$STUB_PYTHON" -c "import sqlite3\n'
        'sqlite3.sqlite_version_info = ${STUB_SQLITE_INFO:-(3, 50, 4)}\n$2"; fi; '
        'if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; '
        'cp "$STUB_VENV_PYTHON" "$3/bin/python"; chmod +x "$3/bin/python"; fi'
    ),
    # macOS `open`: record the URL instead of launching the real browser.
    "open": 'echo "open $*" >> "$STUB_LOG"',
    "sudo": 'echo "sudo $*" >> "$STUB_LOG"; [ "${STUB_SUDO_FAIL:-0}" != 1 ]',
    "installer": (
        'echo "installer $* noninteractive=${NONINTERACTIVE:-}" >> "$STUB_LOG"; '
        '[ "${STUB_INSTALLER_FAIL:-0}" != 1 ] || exit 1; '
        'cp "$STUB_BREW" "$STUB_BIN/brew"; chmod +x "$STUB_BIN/brew"'
    ),
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    log = tmp_path / "calls.log"
    log.touch()

    safehouse = tmp_path / "safehouse"
    safehouse.write_text("#!/bin/bash\necho safehouse 0.11.1\n")
    safehouse.chmod(0o755)
    brew = tmp_path / "brew"
    brew.write_text(BREW)
    brew.chmod(0o755)
    brew_prefix = tmp_path / "brew-prefix"
    (brew_prefix / "bin").mkdir(parents=True)
    for name, body in STUBS.items():
        path = stub_bin / name
        path.write_text(f"#!/bin/bash\nset -eu\n{body}\n")
        path.chmod(0o755)

    home = tmp_path / "home"
    checkout = home / ".corsia"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "backend").mkdir()
    installer = INSTALL.read_text().replace("/opt/homebrew/bin/brew", str(tmp_path / "arm-brew"))
    installer = installer.replace("/usr/local/bin/brew", str(tmp_path / "intel-brew"))
    (checkout / "scripts" / "install.sh").write_text(installer)
    (checkout / "backend" / "requirements.lock").write_text("")
    corsia = checkout / "scripts" / "corsia"
    corsia.write_text(
        "#!/bin/bash\n"
        'read -r answer || answer=none\n'
        'echo "corsia $* stdin=$answer" >> "$STUB_LOG"\n'
        'case "$*" in *--before-start*) exit "${STUB_BEFORE_START_STATUS:-0}" ;; esac\n'
        'case "$*" in *"agent warm"*) exit "${STUB_WARM_STATUS:-0}" ;; esac\n'
    )
    corsia.chmod(0o755)
    browser_source = tmp_path / "agent-browser-source"
    # What `npm install -g agent-browser@0.31` leaves behind, whatever was installed before.
    browser_source.write_text(f"#!/bin/bash\nset -eu\nSTUB_BROWSER_VERSION=0.31.2\n{BROWSER_CLI}\n")
    browser_source.chmod(0o755)
    venv_python = tmp_path / "venv-python"
    venv_python.write_text(
        '#!/bin/bash\nset -eu\n'
        'if [ "${1:-}" = -c ] && [[ "${2:-}" = *has_users* ]]; then\n'
        '  [ "${STUB_HAS_ACCOUNT:-0}" = 1 ]; exit $?\n'
        'fi\n'
        'if [ "${1:-} ${2:-} ${3:-}" = "-m cli init" ]; then\n'
        '  [ "$#" = 7 ] && [ "$4" = --path ] && [ "$5" = "$HOME/corsia" ]\n'
        '  [ "$6" = --setup ] && [ "$7" = local ]\n'
        '  echo "workspace init local defaults" >> "$STUB_LOG"\n'
        '  [ "${STUB_INIT_FAIL:-0}" = 0 ] || exit 1\n'
        '  printf \'{"existing":"preserved"}\\n\' > "$HOME/.corsia/config.json"\n'
        'fi\n'
    )
    answers = tmp_path / "answers"
    answers.write_text("terminal-answer\n")

    return {
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "en_US.UTF-8",
        "STUB_BIN": str(stub_bin),
        "STUB_LOG": str(log),
        "STUB_PYTHON": sys.executable,
        "STUB_VENV_PYTHON": str(venv_python),
        "STUB_SAFEHOUSE": str(safehouse),
        "STUB_BROWSER_SOURCE": str(browser_source),
        "STUB_BREW": str(brew),
        "STUB_BREW_PREFIX": str(brew_prefix),
        "CORSIA_INSTALL_SERVICE_PATH": str(stub_bin),
        "CORSIA_INSTALL_TTY": str(answers),
        "CORSIA_INSTALL_SUDO": str(stub_bin / "sudo"),
        "CORSIA_INSTALL_HOMEBREW_INSTALLER": str(stub_bin / "installer"),
        "CORSIA_INSTALL_BREW": str(stub_bin / "brew"),
    }


def _script(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".corsia" / "scripts" / "install.sh"


def _run(
    env: dict[str, str], *, script: Path | None = None, cwd: Path | None = None, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script or _script(env))],
        cwd=cwd or _script(env).parents[1],
        env={**env, **extra},
        input="",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["STUB_LOG"]).read_text().splitlines()


def _with_brew(env: dict[str, str]) -> None:
    shutil.copy2(env["STUB_BREW"], Path(env["STUB_BIN"]) / "brew")


def _remove_safehouse(env: dict[str, str]) -> None:
    (Path(env["STUB_BIN"]) / "safehouse").unlink()


def _remove(env: dict[str, str], name: str) -> None:
    (Path(env["STUB_BIN"]) / name).unlink()


CLONE_LINE = "Clone CORSIA into ~/.corsia, then run: cd ~/.corsia && bash scripts/install.sh"


def test_running_outside_the_checkout_stops_with_clone_instructions(env, tmp_path):
    result = _run(env, script=INSTALL, cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.strip() == CLONE_LINE


def test_missing_checkout_stops_with_clone_instructions(env, tmp_path):
    missing_env = {**env, "HOME": str(tmp_path / "missing-home")}
    result = _run(missing_env, script=INSTALL, cwd=tmp_path)
    assert result.returncode == 1
    assert result.stderr.strip() == CLONE_LINE


def test_macos_older_than_15_stops_before_installing_anything(env):
    result = _run(env, STUB_MACOS_VERSION="14.7.1")
    assert result.returncode == 1
    assert "CORSIA needs macOS 15 or later (found macOS 14.7.1)." in result.stderr
    assert _calls(env) == []


@pytest.mark.parametrize("version", ["15", "15.0", "26.1"])
def test_macos_15_and_later_pass_the_platform_check(env, version):
    result = _run(env, STUB_MACOS_VERSION=version)
    assert "needs macOS 15" not in result.stderr


FRESH_INSTALL_CALLS = [
    "workspace init local defaults",
    "corsia start stdin=none",
    f"open http://localhost:{DEFAULT_BACKEND_PORT}",
]


def test_a_failed_agent_warm_never_stops_an_existing_accounts_install(env):
    result = _run(env, STUB_WARM_STATUS="1", STUB_HAS_ACCOUNT="1")
    assert result.returncode == 0, result.stderr
    assert _calls(env) == [
        *FRESH_INSTALL_CALLS[:2],
        "corsia sources --install-summary stdin=none",
        "corsia agent warm stdin=none",
        FRESH_INSTALL_CALLS[-1],
    ]


OPENING_LINE = "Opening CORSIA at http:" f"//localhost:{DEFAULT_BACKEND_PORT}"


def test_source_install_opens_browser_setup_without_terminal_input(env):
    result = _run(env, CORSIA_INSTALL_TTY="/nonexistent/no-terminal")
    assert result.returncode == 0, result.stderr
    assert "2/4  Using CORSIA test-version (commit test-sha) in ~/.corsia" in result.stdout
    assert "To update, use Settings › Update." not in result.stdout
    assert not any("brew" in call for call in _calls(env))
    assert _calls(env) == FRESH_INSTALL_CALLS
    assert OPENING_LINE in result.stdout.splitlines()


def test_a_failed_workspace_init_does_not_start_or_open(env):
    result = _run(env, STUB_INIT_FAIL="1")

    assert result.returncode == 1
    assert _calls(env) == FRESH_INSTALL_CALLS[:1]
    assert OPENING_LINE not in result.stdout


def test_a_mac_without_a_model_can_open_browser_setup(env):
    result = _run(env, STUB_BEFORE_START_STATUS="3")

    assert result.returncode == 0, result.stderr
    assert _calls(env) == FRESH_INSTALL_CALLS
    assert OPENING_LINE in result.stdout


def test_configured_workspace_is_preserved_on_stopped_install(env):
    config = Path(env["HOME"]) / ".corsia" / "config.json"
    config.write_text('{"workspace":"existing","setup":"server"}')

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert config.read_text() == '{"workspace":"existing","setup":"server"}'
    assert _calls(env) == FRESH_INSTALL_CALLS[1:]


def test_running_install_shows_the_update_hint_without_restarting(env):
    result = _run(env, STUB_RUNNING="1")
    assert result.returncode == 0, result.stderr
    assert f"CORSIA is running at http://localhost:{DEFAULT_BACKEND_PORT}" in result.stdout
    assert "To update, use Settings › Update." in result.stdout
    assert not any(call.startswith("corsia") for call in _calls(env))


def test_running_install_with_local_flag_runs_the_shared_local_setup(env):
    result = subprocess.run(
        ["/bin/bash", str(_script(env)), "--local"],
        cwd=_script(env).parents[1],
        env={**env, "STUB_RUNNING": "1", "STUB_HAS_ACCOUNT": "1"}, input="", text=True,
        capture_output=True, check=False, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert _calls(env) == ["corsia local setup --automatic --start-only stdin=none"]


def test_installer_leaves_the_assistant_google_tool_to_the_assistant(env):
    _with_brew(env)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert not any("googleworkspace-cli" in call for call in _calls(env))


def test_a_supported_agent_browser_and_gh_are_left_alone(env):
    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert "✓ GitHub CLI" in result.stdout
    assert not any(call.startswith(("npm", "agent-browser", "brew")) for call in _calls(env))


def test_a_missing_gh_is_installed_with_brew_and_no_version_floor(env):
    _with_brew(env)
    _remove(env, "gh")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert "Installing GitHub CLI…" in result.stdout
    assert "brew install gh" in _calls(env)


@pytest.mark.parametrize("installed", [None, "0.39.0", "0.27.4", "0.30.9", "1.31.0"])
def test_a_missing_or_unsupported_agent_browser_is_installed_pinned_then_set_up(env, installed):
    if installed is None:
        _remove(env, "agent-browser")
    result = _run(env, **({"STUB_BROWSER_VERSION": installed} if installed else {}))

    assert result.returncode == 0, result.stderr
    assert "Installing agent-browser…" in result.stdout
    calls = _calls(env)
    assert calls[:2] == ["npm install -g agent-browser@0.31", "agent-browser install"]
    assert calls[2:] == FRESH_INSTALL_CALLS


def test_agent_browser_installs_after_node_is_ready(env):
    _with_brew(env)
    _remove(env, "node")
    _remove(env, "agent-browser")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = _calls(env)
    assert calls.index("brew install node") < calls.index("npm install -g agent-browser@0.31")


def test_an_npm_failure_stops_with_its_real_error_and_the_command_to_retry(env):
    _remove(env, "agent-browser")

    result = _run(env, STUB_NPM_FAIL="1")

    assert result.returncode == 1
    assert "npm ERR! real failure text" in result.stderr
    assert result.stderr.strip().endswith("npm could not install agent-browser. Run: npm install -g agent-browser@0.31")
    assert not any(call.startswith("corsia") for call in _calls(env))


def test_a_failed_browser_setup_stops_the_install(env):
    _remove(env, "agent-browser")

    result = _run(env, STUB_BROWSER_SETUP_FAIL="1")

    assert result.returncode == 1
    assert "Run: agent-browser install" in result.stderr
    assert not any(call.startswith("corsia") for call in _calls(env))


@pytest.mark.parametrize("finder", ["find_brew", "find_installed_brew"])
@pytest.mark.parametrize("available", ["arm", "intel", "both", "none"])
def test_brew_finders_return_the_first_installed_path(env, tmp_path, finder, available):
    arm = tmp_path / "arm-brew"
    intel = tmp_path / "intel-brew"
    for path, kind in ((arm, "arm"), (intel, "intel")):
        if available in (kind, "both"):
            path.write_text("#!/bin/bash\nexit 0\n")
            path.chmod(0o755)
    script = _script(env)

    result = subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; ' + finder, "test", str(script)],
        env={**env, "CORSIA_INSTALL_SOURCE_ONLY": "1", "CORSIA_INSTALL_BREW": ""},
        text=True, capture_output=True, check=False, timeout=10,
    )

    assert result.returncode == (1 if available == "none" else 0), result.stderr
    expected = "" if available == "none" else str(intel if available == "intel" else arm) + "\n"
    assert result.stdout == expected
    assert all(Path(found).resolve().is_relative_to(tmp_path.resolve()) for found in result.stdout.splitlines())


def test_homebrew_uses_the_documented_installer_url():
    source = INSTALL.read_text()
    assert "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh" in source
    assert "HOMEBREW_INSTALLER_SCHEME" not in source


def test_present_brew_installs_missing_items_without_prompt(env):
    _with_brew(env)
    _remove_safehouse(env)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "Installing safehouse…" in result.stdout
    assert "[Y/n]" not in result.stdout
    command = Path(env["STUB_BREW_PREFIX"]) / "bin" / "corsia"
    assert command.is_symlink()
    assert command.resolve() == _script(env).with_name("corsia")
    assert "Stop or start CORSIA any time with: corsia stop / corsia start" in result.stdout
    assert not (Path(env["HOME"]) / ".zprofile").exists()
    assert _calls(env) == [
        "brew install eugene1g/safehouse/agent-safehouse",
        "brew --prefix",
        *FRESH_INSTALL_CALLS,
    ]


@pytest.mark.parametrize("variable, value, formula, label", [
    ("STUB_GIT_VERSION", "2.35.9", "git", "git"),
    ("STUB_SQLITE_INFO", "(3, 44, 2)", "python@3.11", "Python 3.11"),
])
def test_a_git_or_sqlite_below_the_supported_floor_is_not_ready(env, variable, value, formula, label):
    _with_brew(env)
    result = _run(env, **{variable: value})
    assert result.returncode == 1
    assert f"Installing {label}…" in result.stdout
    assert f"Still not ready: {label}" in result.stderr
    assert f"brew install {formula}" in _calls(env)


@pytest.mark.parametrize("variable, value", [
    ("STUB_GIT_VERSION", "2.36.0"), ("STUB_SQLITE_INFO", "(3, 45, 0)"),
])
def test_a_git_or_sqlite_at_the_supported_floor_is_ready(env, variable, value):
    result = _run(env, **{variable: value})
    assert result.returncode == 0, result.stderr
    assert not any("brew" in call for call in _calls(env))


def test_missing_brew_is_installed_then_used_for_missing_items(env):
    _remove_safehouse(env)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    zprofile = Path(env["HOME"]) / ".zprofile"
    brew = env["CORSIA_INSTALL_BREW"]
    assert zprofile.read_text().splitlines() == [f'eval "$({brew} shellenv)"']
    assert "Open a new Terminal window first." in result.stdout
    assert _calls(env) == [
        "sudo -v",
        "installer -c  noninteractive=1",
        "brew install eugene1g/safehouse/agent-safehouse",
        "brew --prefix",
        *FRESH_INSTALL_CALLS,
    ]


def test_fresh_install_with_local_flag_keeps_before_start_setup(env):
    result = subprocess.run(
        ["/bin/bash", str(_script(env)), "--local"],
        cwd=_script(env).parents[1], env=env, input="", text=True,
        capture_output=True, check=False, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert _calls(env) == [
        FRESH_INSTALL_CALLS[0],
        "corsia local setup --before-start stdin=none",
        *FRESH_INSTALL_CALLS[1:],
    ]


def test_foreign_corsia_command_is_left_unchanged_with_a_warning(env):
    _with_brew(env)
    command = Path(env["STUB_BREW_PREFIX"]) / "bin" / "corsia"
    command.symlink_to("/usr/local/bin/corsia")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert command.is_symlink()
    assert command.readlink() == Path("/usr/local/bin/corsia")
    assert (
        f"A different corsia command already exists at {command}; leaving it unchanged."
        in result.stdout
    )
    assert "Stop or start CORSIA any time with: corsia stop / corsia start" not in result.stdout


def test_shellenv_is_added_once_only_when_this_run_installs_homebrew(env):
    _remove_safehouse(env)
    first = _run(env)
    second = _run(env)

    zprofile = Path(env["HOME"]) / ".zprofile"
    brew = env["CORSIA_INSTALL_BREW"]
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert zprofile.read_text().splitlines() == [f'eval "$({brew} shellenv)"']
    assert "Open a new Terminal window first." in first.stdout
    assert "Open a new Terminal window first." not in second.stdout


def test_homebrew_installer_failure_has_one_clear_stop(env):
    _remove_safehouse(env)
    result = _run(env, STUB_INSTALLER_FAIL="1")
    assert result.returncode == 1
    assert result.stderr.strip() == "Homebrew installation failed. Fix Homebrew, then run this again."


def test_sudo_refusal_has_one_clear_stop(env):
    _remove_safehouse(env)
    result = _run(env, STUB_SUDO_FAIL="1")
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Homebrew needs an administrator account. Sign in with an administrator account, then run this again."
    )
    assert not any(call.startswith("installer") for call in _calls(env))


def test_formula_failure_has_one_clear_stop(env):
    _with_brew(env)
    _remove_safehouse(env)
    result = _run(env, STUB_BREW_FAIL="1")
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Homebrew could not install safehouse. Run: brew install eugene1g/safehouse/agent-safehouse"
    )
