"""The Linux branch of the post-clone installer, exercised only with fake local commands.

The PATH holds the stubs and a few real tools, never the machine's own git, gh,
node or python, so a runner that has them cannot change a result. The macOS
branch has its own file (test_install_script.py, run on the macOS job); the
last tests here only prove the dispatch leaves it alone.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from infra.default_port import DEFAULT_BACKEND_PORT
from tools.managed_runtime import RUNTIME_PINS

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install.sh"
APP_URL = f"http://localhost:{DEFAULT_BACKEND_PORT}"

NODE_VERSION = RUNTIME_PINS["node"]["arm64"].version
UV_VERSION = RUNTIME_PINS["uv"]["arm64"].version
NODE_URL = {
    "aarch64": f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-arm64.tar.xz",
    "x86_64": f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz",
}
UV_URL = {
    arch: f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{arch}-unknown-linux-gnu.tar.gz"
    for arch in ("aarch64", "x86_64")
}

# The real commands the script and its fixtures need; nothing else leaks in.
REAL_TOOLS = (
    "bash sh env mkdir rm ln cp mv cat tar xz gzip install mktemp dirname basename readlink "
    "grep head tail sed awk tr sort chmod touch ls cut sha256sum"
).split()

APT_BASE = "build-essential curl ca-certificates xz-utils unzip jq sqlite3 python3 lsof"
APT_ENV = "env DEBIAN_FRONTEND=noninteractive apt-get"

GIT = (
    'case "$*" in *--version*) echo "git version ${STUB_GIT_VERSION:-2.43.0}" ;; '
    '*describe*) echo test-version ;; *rev-parse*) echo test-sha ;; esac'
)
GH = 'echo gh version 2.45.0'
PYTHON = (
    'if [ "$1" = -c ]; then exec "$STUB_PYTHON" -c "import sqlite3\n'
    'sqlite3.sqlite_version_info = ${STUB_SQLITE_INFO:-(3, 50, 4)}\n$2"; fi; '
    'if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; '
    "printf '#!/bin/bash\\nexit 0\\n' > \"$3/bin/python\"; chmod +x \"$3/bin/python\"; fi"
)
NODE = 'echo v${STUB_NODE_VERSION:-26.12.0}'
UV = (
    'echo "uv $*" >> "$STUB_LOG"; '
    'if [ "$1 $2" = "python find" ]; then echo "$STUB_MANAGED_PYTHON"; fi'
)

STUBS = {
    "uname": 'case "$1" in -s) echo "${STUB_OS:-Linux}" ;; -m) echo "${STUB_ARCH:-aarch64}" ;; esac',
    "id": 'if [ "$1" = -un ]; then echo tester; else echo "${STUB_UID:-1000}"; fi',
    "loginctl": (
        'if [ "$1" = show-user ]; then '
        'if [ -f "$STUB_LINGER_FILE" ]; then echo yes; else echo "${STUB_LINGER:-yes}"; fi; '
        'else echo "loginctl $*" >> "$STUB_LOG"; touch "$STUB_LINGER_FILE"; fi'
    ),
    "sw_vers": 'echo "${STUB_MACOS_VERSION:-14.7.1}"',
    "dpkg-query": (
        'for last; do :; done; '
        'case " ${STUB_DPKG_INSTALLED:-} " in *" $last "*) printf "install ok installed" ;; *) exit 1 ;; esac'
    ),
    "xdg-open": 'echo "xdg-open $*" >> "$STUB_LOG"',
    "sudo": 'echo "sudo $*" >> "$STUB_LOG"; [ "${STUB_SUDO_FAIL:-0}" != 1 ] || exit 1; [ "$1" = -v ] || "$@"',
    # What apt would leave behind: git and gh are the only installs a stub can show.
    "apt-get": (
        'echo "apt-get $*" >> "$STUB_LOG"; '
        'case "$*" in install*) for package in "$@"; do '
        'if [ -f "$STUB_TEMPLATES/$package" ]; then cp "$STUB_TEMPLATES/$package" "$STUB_BIN/$package"; fi; '
        "done ;; esac"
    ),
    "curl": (
        'case "$*" in *api/health*) [ "${STUB_RUNNING:-0}" = 1 ]; exit ;; esac; '
        'out=""; url=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac; shift; done; '
        'echo "curl $url" >> "$STUB_LOG"; cp "$STUB_DOWNLOADS/$(basename "$url")" "$out"'
    ),
}


def _tarball(path: Path, mode: str, files: dict[str, str]) -> str:
    with tarfile.open(path, mode) as archive:  # type: ignore[call-overload]
        for name, body in files.items():
            data = f"#!/bin/bash\n{body}\n".encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stub_bin = tmp_path / "stub-bin"
    tools = tmp_path / "tools"
    templates = tmp_path / "templates"
    downloads = tmp_path / "downloads"
    for directory in (stub_bin, tools, templates, downloads):
        directory.mkdir()
    log = tmp_path / "calls.log"
    log.touch()

    for name in REAL_TOOLS:
        real = shutil.which(name)
        if real:
            (tools / name).symlink_to(real)
    if not (tools / "sha256sum").exists():  # macOS ships shasum, not sha256sum
        (stub_bin / "sha256sum").write_text(
            '#!/bin/bash\nread -r digest file\n'
            '[ "$digest" = "$(shasum -a 256 "$file" | cut -d" " -f1)" ]\n'
        )
        (stub_bin / "sha256sum").chmod(0o755)

    for name, body in STUBS.items():
        path = stub_bin / name
        path.write_text(f"#!/bin/bash\nset -eu\n{body}\n")
        path.chmod(0o755)
    # Prerequisites a test can make present with _present; none is on the PATH by default.
    for name, body in {"git": GIT, "gh": GH, "python3.11": PYTHON, "node": NODE}.items():
        (templates / name).write_text(f"#!/bin/bash\nset -eu\n{body}\n")
        (templates / name).chmod(0o755)
    # The fixtures behind the pinned downloads.
    managed_python = tmp_path / "managed-python"
    managed_python.write_text(f"#!/bin/bash\nset -eu\n{PYTHON}\n")
    managed_python.chmod(0o755)
    shas = {}
    for arch, url in NODE_URL.items():
        shas[("node", arch)] = _tarball(
            downloads / url.rsplit("/", 1)[1], "w:xz",
            {f"node-v{NODE_VERSION}-linux-{'arm64' if arch == 'aarch64' else 'x64'}/bin/{tool}": NODE for tool in ("node", "npm", "npx")},
        )
    for arch, url in UV_URL.items():
        shas[("uv", arch)] = _tarball(
            downloads / url.rsplit("/", 1)[1], "w:gz",
            {f"uv-{arch}-unknown-linux-gnu/uv": UV, f"uv-{arch}-unknown-linux-gnu/uvx": "exit 0"},
        )

    home = tmp_path / "home"
    checkout = home / ".corsia"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "backend").mkdir()
    shutil.copytree(REPO / "backend" / "infra", checkout / "backend" / "infra")
    (checkout / "backend" / "tools").mkdir()
    runtime = checkout / "backend" / "tools" / "managed_runtime.py"
    shutil.copy2(REPO / "backend" / "tools" / "managed_runtime.py", runtime)
    with runtime.open("a") as file:
        file.write('''\n# Test-only local archive transport; the real verifier and extractor remain in use.
from dataclasses import replace
for _kind in ("node", "uv"):
    for _arch in ("arm64", "x86_64"):
        _key = "linux-" + _arch
        _pin_value = RUNTIME_PINS[_kind][_key]
        _digest = os.environ["STUB_SHA_" + _kind.upper() + "_" + _arch.upper()]
        if os.environ.get("STUB_BAD_CHECKSUM") == _kind:
            _digest = "0" * 64
        RUNTIME_PINS[_kind][_key] = replace(_pin_value, sha256=_digest)
def _download(url, destination):
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write("curl " + url + "\\n")
    source = Path(os.environ["STUB_DOWNLOADS"]) / url.rsplit("/", 1)[-1]
    shutil.copyfile(source, destination)
    return hashlib.sha256(destination.read_bytes()).hexdigest()
''')
    (tools / "python3").symlink_to(sys.executable)
    shutil.copy2(INSTALL, checkout / "scripts" / "install.sh")
    (checkout / "backend" / "requirements.lock").write_text("")
    corsia = checkout / "scripts" / "corsia"
    corsia.write_text(
        "#!/bin/bash\n"
        'read -r answer || answer=none\n'
        'echo "corsia $* stdin=$answer" >> "$STUB_LOG"\n'
    )
    corsia.chmod(0o755)
    answers = tmp_path / "answers"
    answers.write_text("terminal-answer\n")
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.4 LTS"\n')

    return {
        "PATH": f"{stub_bin}:{tools}",
        "HOME": str(home),
        "STUB_BIN": str(stub_bin),
        "STUB_LOG": str(log),
        "STUB_LINGER_FILE": str(tmp_path / "linger-enabled"),
        "STUB_PYTHON": sys.executable,
        "STUB_TEMPLATES": str(templates),
        "STUB_DOWNLOADS": str(downloads),
        "STUB_MANAGED_PYTHON": str(managed_python),
        "STUB_DPKG_INSTALLED": "",
        "CORSIA_INSTALL_SERVICE_PATH": f"{home}/.local/bin:{stub_bin}",
        "CORSIA_INSTALL_TTY": str(answers),
        "CORSIA_INSTALL_SUDO": str(stub_bin / "sudo"),
        "CORSIA_INSTALL_OS_RELEASE": str(os_release),
        "STUB_SHA_NODE_ARM64": shas[("node", "aarch64")],
        "STUB_SHA_NODE_X86_64": shas[("node", "x86_64")],
        "STUB_SHA_UV_ARM64": shas[("uv", "aarch64")],
        "STUB_SHA_UV_X86_64": shas[("uv", "x86_64")],
    }


def _script(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".corsia" / "scripts" / "install.sh"


def _run(env: dict[str, str], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(_script(env)), *args],
        cwd=_script(env).parents[1], env={**env, **extra}, input="", text=True,
        capture_output=True, check=False, timeout=30,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["STUB_LOG"]).read_text().splitlines()


def _present(env: dict[str, str], *names: str) -> None:
    """Make a prerequisite already installed, as the stubs behave when it is."""
    for name in names:
        shutil.copy2(Path(env["STUB_TEMPLATES"]) / name, Path(env["STUB_BIN"]) / name)


def _everything_installed(env: dict[str, str]) -> dict[str, str]:
    _present(env, "git", "gh", "python3.11", "node")
    return {**env, "STUB_DPKG_INSTALLED": f"git gh {APT_BASE}"}


def _local_bin(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".local" / "bin"


FRESH_CALLS = [
    "sudo -v",
    f"sudo {APT_ENV} update -qq",
    "apt-get update -qq",
    f"sudo {APT_ENV} install -y {APT_BASE} git gh",
    f"apt-get install -y {APT_BASE} git gh",
    f"curl {UV_URL['aarch64']}",
    "uv python install 3.11",
    "uv python find 3.11",
    f"curl {NODE_URL['aarch64']}",
    "corsia start stdin=terminal-answer",
    "corsia sources --install-summary stdin=none",
]


def test_a_fresh_ubuntu_installs_apt_packages_uv_python_and_node_then_starts(env):
    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _calls(env) == FRESH_CALLS
    assert "This needs administrator access (sudo)" in result.stdout
    local_bin = _local_bin(env)
    assert (local_bin / "python3.11").resolve() == Path(env["STUB_MANAGED_PYTHON"]).resolve()
    for tool in ("node", "npm", "npx"):
        assert (local_bin / tool).resolve().parent.parent.name == f"node-v{NODE_VERSION}-linux-arm64"
    assert (local_bin / "uv").is_file()
    assert (local_bin / "corsia").resolve() == _script(env).with_name("corsia").resolve()
    assert (Path(env["HOME"]) / ".corsia" / ".venv" / "bin" / "python").exists()
    assert f"CORSIA is running at {APP_URL}" in result.stdout
    assert "There is no local model on Linux" in result.stdout


def test_x86_64_downloads_the_x86_64_builds(env):
    # The fixture pins each architecture to its own local archive.
    result = _run(env, STUB_ARCH="x86_64")

    assert result.returncode == 0, result.stderr
    curls = [call for call in _calls(env) if call.startswith("curl ")]
    assert curls == [f"curl {UV_URL['x86_64']}", f"curl {NODE_URL['x86_64']}"]


def test_a_second_run_changes_nothing_and_never_asks_for_sudo(env):
    assert _run(env).returncode == 0
    Path(env["STUB_LOG"]).write_text("")
    # What the first run left behind: its packages, and git and gh from apt.
    installed = {**env, "STUB_DPKG_INSTALLED": f"git gh {APT_BASE}"}

    result = _run(installed)

    assert result.returncode == 0, result.stderr
    assert _calls(env) == [
        "corsia start stdin=terminal-answer",
        "corsia sources --install-summary stdin=none",
    ]


def test_supported_tools_already_present_are_left_alone(env):
    result = _run(_everything_installed(env))

    assert result.returncode == 0, result.stderr
    assert not any(call.startswith(("curl", "uv", "sudo", "apt-get")) for call in _calls(env))
    assert not (_local_bin(env) / "uv").exists()


def test_a_python_with_an_old_sqlite_is_replaced_by_the_managed_one(env):
    # The managed python here is the same stub, so the recheck still sees the old SQLite.
    result = _run(_everything_installed(env), STUB_SQLITE_INFO="(3, 44, 2)")

    assert result.returncode == 1
    assert "uv python install 3.11" in _calls(env)
    assert result.stderr.strip().startswith("Still not ready: Python 3.11")


def test_a_download_that_does_not_match_its_checksum_is_refused_and_unused(env):
    result = _run(env, STUB_BAD_CHECKSUM="node")

    assert result.returncode == 1
    assert "Couldn’t verify the node download. Run this again." in result.stderr
    assert "Traceback" not in result.stderr
    assert not (_local_bin(env) / "node").exists()
    assert not any(call.startswith("corsia") for call in _calls(env))


def test_sudo_is_used_for_apt_only(env):
    _run(env)
    sudo_calls = [call for call in _calls(env) if call.startswith("sudo")]
    assert sudo_calls
    assert all(" apt-get " in call or call == "sudo -v" for call in sudo_calls)


def test_linger_is_enabled_once_with_a_notice_and_undo(env):
    first = _run(_everything_installed(env), STUB_LINGER="no")
    assert first.returncode == 0, first.stderr
    assert "sudo loginctl disable-linger tester" in first.stdout
    assert "loginctl enable-linger tester" in _calls(env)
    Path(env["STUB_LOG"]).write_text("")
    second = _run(_everything_installed(env), STUB_LINGER="no")
    assert second.returncode == 0, second.stderr
    assert not any("enable-linger" in call for call in _calls(env))


def test_a_refused_sudo_has_one_clear_stop(env):
    result = _run(env, STUB_SUDO_FAIL="1")

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Installing system packages needs an administrator account. Sign in with one, then run this again."
    )
    assert not any("apt-get" in call for call in _calls(env))


def test_root_runs_apt_without_sudo(env):
    result = _run(env, STUB_UID="0")

    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("sudo") for call in _calls(env))
    assert f"apt-get install -y {APT_BASE} git gh" in _calls(env)


@pytest.mark.parametrize("release", [
    'ID=fedora\nPRETTY_NAME="Fedora Linux 42"\n',
    'ID=arch\nPRETTY_NAME="Arch Linux"\n',
    "",
])
def test_a_distro_without_apt_is_refused_before_anything_installs(env, release):
    Path(env["CORSIA_INSTALL_OS_RELEASE"]).write_text(release)

    result = _run(env)

    assert result.returncode == 1
    assert result.stderr.strip() == "CORSIA needs Ubuntu 24.04 or later, or Debian 12 or later."
    assert _calls(env) == []


def test_debian_12_is_accepted(env):
    Path(env["CORSIA_INSTALL_OS_RELEASE"]).write_text('ID=debian\nVERSION_ID="12"\n')
    assert _run(env).returncode == 0


@pytest.mark.parametrize("distro, version", [("ubuntu", "22.04"), ("debian", "11"), ("ubuntu", "")])
def test_older_and_unknown_versions_are_refused_before_install(env, distro, version):
    Path(env["CORSIA_INSTALL_OS_RELEASE"]).write_text(f'ID={distro}\nVERSION_ID="{version}"\n')
    result = _run(env)
    assert result.returncode == 1
    assert "Ubuntu 24.04 or later, or Debian 12 or later" in result.stderr
    assert _calls(env) == []


def test_an_unsupported_cpu_is_refused(env):
    result = _run(env, STUB_ARCH="riscv64")
    assert result.returncode == 1
    assert result.stderr.strip() == "CORSIA installs on Linux x86_64 or arm64 only (found riscv64)."
    assert _calls(env) == []


def test_the_local_model_flag_is_refused_on_linux(env):
    result = _run(env, "--local")
    assert result.returncode == 1
    assert "no local model on Linux" in result.stderr
    assert _calls(env) == []


def test_a_running_install_shows_the_update_hint_without_restarting(env):
    result = _run(_everything_installed(env), STUB_RUNNING="1")

    assert result.returncode == 0, result.stderr
    assert f"CORSIA is running at {APP_URL}" in result.stdout
    assert "To update, use Settings › Update." in result.stdout
    assert not any(call.startswith("corsia") for call in _calls(env))


def test_a_foreign_corsia_command_is_left_unchanged_with_a_warning(env):
    command = _local_bin(env) / "corsia"
    command.parent.mkdir(parents=True)
    command.symlink_to("/usr/local/bin/corsia")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert command.readlink() == Path("/usr/local/bin/corsia")
    assert f"A different corsia command is already at {command}." in result.stdout


def test_the_corsia_command_note_says_how_to_reach_it_when_the_bin_dir_is_new(env):
    result = _run(env)
    assert "To use the corsia command, log out and back in" in result.stdout
    on_path = {**env, "PATH": f"{_local_bin(env)}:{env['PATH']}"}
    assert "To use the corsia command" not in _run(on_path).stdout


def test_a_display_opens_the_browser_and_a_headless_box_prints_the_address(env):
    headless = _run(env)
    assert "Open that address in a browser." in headless.stdout
    assert not any(call.startswith("xdg-open") for call in _calls(env))

    shutil.rmtree(Path(env["HOME"]) / ".local")
    desktop = _run(env, DISPLAY=":0")
    assert f"xdg-open {APP_URL}" in _calls(env)
    assert f"Opening CORSIA at {APP_URL}" in desktop.stdout


def test_the_linux_branch_never_touches_the_mac_only_steps(env):
    _run(env)
    joined = "\n".join(_calls(env))
    for mac_only in ("brew", "safehouse", "npm", "agent-browser", "local setup", "agent warm"):
        assert mac_only not in joined


def test_linux_pins_are_only_in_the_managed_runtime():
    source = INSTALL.read_text()
    for kind in ("node", "uv"):
        for arch in ("arm64", "x86_64"):
            pin = RUNTIME_PINS[kind][f"linux-{arch}"]
            assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256)
            assert pin.sha256 not in source and pin.url not in source
    assert "managed_runtime_linux" in source


def test_darwin_still_takes_the_macos_path(env):
    """The dispatch hands only Linux to the new branch; macOS reaches its own checks first."""
    result = _run(env, STUB_OS="Darwin", STUB_ARCH="arm64")

    assert result.returncode == 1
    assert "CORSIA needs macOS 15 or later (found macOS 14.7.1)." in result.stderr
    assert _calls(env) == []


def test_another_os_is_still_refused_by_the_original_message(env):
    result = _run(env, STUB_OS="FreeBSD", STUB_ARCH="amd64")
    assert result.returncode == 1
    assert result.stderr.strip() == "CORSIA installs on macOS with Apple Silicon only (found FreeBSD amd64)."
