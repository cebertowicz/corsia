"""Ensure managed setup has an oMLX executable without modifying an existing one."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

from llm import managed_omlx_binary as managed
from llm.omlx_release import OMLX_SHA256, OMLX_VERSION

_BROKEN_RETURN_CODES = (126, 127)
_broken_cache: dict[tuple, bool] = {}
PROBE_TIMEOUT_SECONDS = 15
STREAM_COMMAND_OUTPUT = False
STREAMING = threading.Event()
_COMMAND_ENV = {"HOMEBREW_NO_ASK": "1"}
_LEGACY_SETUP_MARKER = "local_setup_installed_omlx"


class OwnOmlxUpgradeRequired(RuntimeError):
    """A person's working executable needs their update before managed setup can use it."""


def _is_broken_leftover(path: str) -> bool:
    try:
        key = (path, Path(path).stat().st_mtime_ns)
    except OSError:
        return True
    if key in _broken_cache:
        return _broken_cache[key]
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        broken = True
    else:
        broken = (result.returncode in _BROKEN_RETURN_CODES
                  or "bad interpreter" in (result.stderr or "").lower())
    _broken_cache.clear()
    _broken_cache[key] = broken
    return broken


def omlx_on_path() -> str | None:
    """Locate the person's PATH executable without running it."""
    path = shutil.which("omlx")
    return None if path is None or managed.is_managed_omlx_executable(path) else path


def legacy_setup_marker_path() -> Path:
    """Locate the marker written by published pre-record setup installs."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _LEGACY_SETUP_MARKER


def legacy_setup_is_foreign() -> bool:
    """Keep the legacy record migration off an unmarked or replaced oMLX install."""
    current = omlx_on_path()
    marker = legacy_setup_marker_path()
    if current is None or not marker.exists():
        return current is not None
    brew = shutil.which("brew")
    if not brew:
        return True
    try:
        versions = managed.command_output([brew, "list", "--versions", managed.OMLX_FORMULA])
        prefix = managed.command_output([brew, "--prefix", managed.OMLX_FORMULA])
        return not bool(versions and prefix and Path(current).resolve().is_relative_to(Path(prefix).resolve()))
    except (OSError, subprocess.SubprocessError):
        return True


def legacy_omlx_settings_file() -> Path:
    """Find the pre-redesign oMLX settings used to prove its completed starter."""
    bootstrap = Path.home() / "Library" / "Application Support" / "oMLX" / "base-path"
    try:
        moved = bootstrap.read_text(encoding="utf-8").strip()
    except OSError:
        moved = ""
    base = Path(moved).expanduser() if moved else Path.home() / ".omlx"
    return base / "settings.json"


def run_command(command: list[str]) -> None:
    """Run an installer command, streaming only when the CLI owns the terminal."""
    env = {**os.environ, **_COMMAND_ENV}
    if STREAM_COMMAND_OUTPUT:
        STREAMING.set()
        try:
            subprocess.run(command, check=True, text=True, env=env)
        finally:
            STREAMING.clear()
        return
    subprocess.run(command, check=True, capture_output=True, text=True, env=env)


def installer_failure_message(tool: str, action: str, exc: OSError | subprocess.SubprocessError) -> str:
    """Return the failing install step's last stderr line, bounded for the card."""
    stderr = str(getattr(exc, "stderr", "") or "")
    detail = next((line.strip()[:300] for line in reversed(stderr.splitlines()) if line.strip()), "")
    prefix = f"{tool} couldn't {action} oMLX"
    return f"{prefix}: {detail}. Try again." if detail else f"{prefix}. Try again."


def install_omlx() -> None:
    """Install the bundle's verified wheel into a private environment."""
    wheel_name = f"omlx-{OMLX_VERSION}-cp311-cp311-macosx_15_0_universal2.whl"
    url = f"https://github.com/jundot/omlx/releases/download/v{OMLX_VERSION}/{wheel_name}"
    environment = managed.source_omlx_environment()
    with tempfile.TemporaryDirectory(prefix="corsia-omlx-") as directory:
        wheel = Path(directory) / wheel_name
        steps = [
            ("curl", "download", ["curl", "--fail", "--location", "--silent", "--show-error", url, "--output", str(wheel)]),
            ("Python", "prepare", [sys.executable, "-m", "venv", str(environment)]),
            ("pip", "install", [str(environment / "bin" / "python"), "-m", "pip", "install", "--no-cache-dir", str(wheel)]),
        ]
        for index, (tool, action, command) in enumerate(steps):
            try:
                if index == 1:
                    with wheel.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                    if digest != OMLX_SHA256:
                        raise RuntimeError("The downloaded oMLX wheel failed its SHA-256 check. Try again.")
                run_command(command)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(installer_failure_message(tool, action, exc)) from exc
        (environment / ".installed").touch()


def ensure_omlx(progress: Callable[[str], None], *, needs_repair: bool = False) -> None:
    """Use the bundled executable, install an absent one, or repair a managed source install."""
    install_error = managed.managed_omlx_install_error()
    if install_error:
        raise RuntimeError(install_error)
    if managed.managed_omlx_executable() is not None:
        return
    current = omlx_on_path()
    if current is not None:
        if needs_repair or _is_broken_leftover(current):
            raise OwnOmlxUpgradeRequired(
                "Run brew upgrade jundot/omlx/omlx in Terminal (or brew reinstall jundot/omlx/omlx "
                "for a broken installation), then try again; or install the CORSIA app to use its bundled oMLX."
            )
        return
    progress("Installing oMLX")
    install_omlx()


_LOG_ERROR = re.compile(r"^(ERROR|CRITICAL)\b| - (ERROR|CRITICAL) - |^\w*(Error|Exception):")


def omlx_log_detail() -> str:
    """Return a bounded source-install log error, without consulting brew in a packaged app."""
    if managed.managed_omlx_executable() is not None:
        return ""
    brew = shutil.which("brew")
    if not brew:
        return ""
    path = Path(brew).parent.parent / "var" / "log" / "omlx.log"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    errors = [line.strip() for line in lines if _LOG_ERROR.search(line.strip())]
    return f" oMLX's log says: {errors[-1][:300]} (full log: {path})" if errors else f" oMLX's log: {path}"
