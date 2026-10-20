"""Ensure managed setup has an oMLX executable without modifying an existing one."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from llm import managed_omlx_binary as managed

_BROKEN_RETURN_CODES = (126, 127)
_broken_cache: dict[tuple, bool] = {}
PROBE_TIMEOUT_SECONDS = 15
STREAM_COMMAND_OUTPUT = False
STREAMING = threading.Event()
_COMMAND_ENV = {"HOMEBREW_NO_ASK": "1"}


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


def homebrew_failure_message(action: str, exc: OSError | subprocess.SubprocessError) -> str:
    """Return card-safe Homebrew repair feedback."""
    stderr = str(getattr(exc, "stderr", "") or "")
    detail = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    prefix = f"Homebrew couldn't {action} oMLX"
    return f"{prefix}: {detail}. Try again." if detail else f"{prefix}. Try again."


def ensure_omlx(progress: Callable[[str], None], *, needs_repair: bool = False, broken: bool = False) -> None:
    """Use the bundled executable, install an absent one, or repair a managed source install."""
    if managed.managed_omlx_executable() is not None:
        return
    current = omlx_on_path()
    brew = shutil.which("brew")
    if current is not None:
        if needs_repair or _is_broken_leftover(current):
            raise OwnOmlxUpgradeRequired(
                "Run `brew upgrade jundot/omlx/omlx` (or `brew reinstall jundot/omlx/omlx` "
                "for a broken installation), then try again; or install the CORSIA app to use its bundled oMLX."
            )
        return
    if current is None:
        if not brew:
            raise RuntimeError("Homebrew is required to install oMLX. Install Homebrew, then retry.")
        progress("Installing oMLX")
        try:
            run_command([brew, "tap", "jundot/omlx", "https://github.com/jundot/omlx"])
            try:
                run_command([brew, "install", managed.OMLX_FORMULA])
            except (OSError, subprocess.SubprocessError) as exc:
                if not managed.command_output([brew, "list", "--versions", managed.OMLX_FORMULA]):
                    raise exc
                run_command([brew, "link", "--overwrite", "omlx"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(homebrew_failure_message("install", exc)) from exc
        return


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
