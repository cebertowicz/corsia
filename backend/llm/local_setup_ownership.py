"""Install and repair only the oMLX executable whose ownership is proven.

Identity belongs to local_setup_managed. Setup never upgrades a person's
binary; its install record and current Homebrew keg must both prove ownership.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from llm.local_setup_managed import (  # noqa: F401 — compatibility for existing installer callers
    BREW_SERVICE_LABEL,
    MARKER_NAME,
    launchd_jobs,
    legacy_setup_owns_current_omlx,
    mark_installed_by_setup,
    marker_path,
    setup_owns_current_omlx,
)

OMLX_FORMULA = "jundot/omlx/omlx"
log = logging.getLogger(__name__)
_BROKEN_RETURN_CODES = (126, 127)
_broken_cache: dict[tuple, bool] = {}
PROBE_TIMEOUT_SECONDS = 15


class OwnOmlxUpgradeRequired(RuntimeError):
    """The executable belongs to the person and needs their update before setup."""


def _is_broken_leftover(path: str) -> bool:
    """Cache settled entry-point failures by path and modification time."""
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
    """Locate the executable without running it; safe for status projections."""
    return shutil.which("omlx")


def installed_omlx() -> str | None:
    """Return a runnable oMLX, or None for an absent/broken leftover entry point."""
    path = omlx_on_path()
    return None if path is None or _is_broken_leftover(path) else path


STREAM_COMMAND_OUTPUT = False
STREAMING = threading.Event()
_COMMAND_ENV = {"HOMEBREW_NO_ASK": "1"}


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


def command_output(command: list[str]) -> str:
    """Run a bounded Homebrew inspection and return its non-secret output."""
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def retire_legacy_owned_omlx_service() -> bool:
    """Stop only the prior service positively identified as CORSIA's."""
    if not legacy_setup_owns_current_omlx():
        return False
    brew = shutil.which("brew")
    if not brew:
        return False
    try:
        run_command([brew, "services", "stop", OMLX_FORMULA])
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("could not retire CORSIA's old oMLX service: %s", error)
        return False
    return True


def legacy_omlx_settings_file() -> Path:
    """Read the old service's base-path bootstrap only after ownership is proven."""
    bootstrap = Path.home() / "Library" / "Application Support" / "oMLX" / "base-path"
    try:
        moved = bootstrap.read_text(encoding="utf-8").strip()
    except OSError:
        moved = ""
    base = Path(moved).expanduser() if moved else Path.home() / ".omlx"
    return base / "settings.json"


def homebrew_failure_message(action: str, exc: OSError | subprocess.SubprocessError) -> str:
    """Return card-safe Homebrew repair feedback without command arguments."""
    stderr = str(getattr(exc, "stderr", "") or "")
    detail = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    prefix = f"Homebrew couldn't {action} oMLX"
    return f"{prefix}: {detail}. Try again." if detail else f"{prefix}. Try again."


def ensure_omlx(progress: Callable[[str], None], *, needs_repair: bool = False,
                broken: bool = False) -> None:
    """Install missing oMLX, and repair only a binary setup demonstrably owns."""
    current = installed_omlx()
    existing = omlx_on_path()
    if current is None and existing is not None:
        current, needs_repair, broken = existing, True, True
    brew = shutil.which("brew")
    if current is None:
        if not brew:
            raise RuntimeError("Homebrew is required to install oMLX. Install Homebrew, then retry.")
        progress("Installing oMLX")
        try:
            run_command([brew, "tap", "jundot/omlx", "https://github.com/jundot/omlx"])
            try:
                run_command([brew, "install", OMLX_FORMULA])
            except (OSError, subprocess.SubprocessError) as exc:
                if not command_output([brew, "list", "--versions", OMLX_FORMULA]):
                    raise exc
                run_command([brew, "link", "--overwrite", "omlx"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(homebrew_failure_message("install", exc)) from exc
        mark_installed_by_setup()
        return
    if not needs_repair:
        return
    try:
        owned = bool(brew) and setup_owns_current_omlx(str(brew), current)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(homebrew_failure_message("check", exc)) from exc
    if not owned:
        raise OwnOmlxUpgradeRequired("Update your oMLX, then Retry.")
    progress("Updating oMLX")
    try:
        run_command([str(brew), "tap", "jundot/omlx", "https://github.com/jundot/omlx"])
        run_command([str(brew), "update"])
        run_command([str(brew), "reinstall" if broken else "upgrade", OMLX_FORMULA])
    except (OSError, subprocess.SubprocessError) as exc:
        action = "reinstall" if broken else "update"
        raise RuntimeError(homebrew_failure_message(action, exc)) from exc


_LOG_ERROR = re.compile(r"^(ERROR|CRITICAL)\b| - (ERROR|CRITICAL) - |^\w*(Error|Exception):")


def omlx_log_detail() -> str:
    """Return the last Homebrew service log error as a message clause, or an empty string."""
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
    if errors:
        return f" oMLX's log says: {errors[-1][:300]} (full log: {path})"
    return f" oMLX's log: {path}"
