"""Whether this machine's oMLX is one local setup installed, or the person's own.

Automatic setup may configure an oMLX it installed itself. It never writes to
one it did not: a marker written at install time and the current binary's
resolved Homebrew formula location together prove setup ownership.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from llm.local_setup_managed import MANAGED_DIR

MARKER_NAME = "local_setup_installed_omlx"
OMLX_FORMULA = "jundot/omlx/omlx"
#: The launchd job ``brew services`` runs the oMLX formula as.
BREW_SERVICE_LABEL = "homebrew.mxcl.omlx"

log = logging.getLogger(__name__)


def marker_path() -> Path:
    """Legacy v0.4 ownership marker, retained only to migrate CORSIA's old service."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / MARKER_NAME



_BROKEN_RETURN_CODES = (126, 127)
_broken_cache: dict[tuple, bool] = {}
PROBE_TIMEOUT_SECONDS = 15


def _is_broken_leftover(path: str) -> bool:
    """Whether ``path`` is an entry point that cannot run at all (an old uninstall's remains).

    A start that times out is slow, not broken. Only a settled answer is cached,
    per path and modification time, so status polls do not re-run the binary.
    """
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



def installed_omlx() -> str | None:
    """The working ``omlx`` on PATH, or None when it is absent or a broken leftover."""
    path = shutil.which("omlx")
    return None if path is None or _is_broken_leftover(path) else path


# The installer sets this so Homebrew's own output reaches its terminal; the
# server keeps capturing it, so a failure's last stderr line names the cause.
STREAM_COMMAND_OUTPUT = False
# Set while a streamed command owns the terminal, so the installer adds no lines over it.
STREAMING = threading.Event()
# Homebrew 7 asks "[y/n]" before installing dependencies whenever it has a terminal.
_COMMAND_ENV = {"HOMEBREW_NO_ASK": "1"}


def run_command(command: list[str]) -> None:
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
    """Run a bounded Homebrew inspection command and return its non-secret output."""
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout.strip()



def setup_owns_current_omlx(brew: str, omlx: str) -> bool:
    """Whether the marked executable resolves inside CORSIA's old Homebrew keg.

    The marker alone is not ownership proof: it could survive a user replacing
    the binary. The old install is migratable only when Homebrew still reports
    the formula and the current executable resolves below that formula's keg.
    """
    if not command_output([brew, "list", "--versions", OMLX_FORMULA]):
        return False
    prefix = command_output([brew, "--prefix", OMLX_FORMULA])
    return bool(prefix) and Path(omlx).resolve().is_relative_to(Path(prefix).resolve())


def legacy_setup_owns_current_omlx() -> bool:
    """Whether the prior Homebrew service is positively identified as CORSIA's."""
    # The marker is checked first, so an install without it runs nothing.
    if not marker_path().exists():
        return False
    brew, omlx = shutil.which("brew"), installed_omlx()
    if not brew or not omlx:
        return False
    try:
        return setup_owns_current_omlx(brew, omlx)
    except (OSError, subprocess.SubprocessError):
        return False


def retire_legacy_owned_omlx_service() -> bool:
    """Stop only the prior service that the v0.3/v0.4 proof identifies as ours.

    A failure leaves the private managed launch safe: its port and paths are
    distinct, and the old instance's row is retired at startup.
    """
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
    """Where CORSIA's old Homebrew oMLX kept ``settings.json``; read only after ownership is proven.

    oMLX's macOS app writes a one-line ``base-path`` file when the person moves
    its data root, and oMLX reads it before falling back to ``~/.omlx`` (oMLX
    v0.7.0 ``omlx/settings.py``).
    """
    bootstrap = Path.home() / "Library" / "Application Support" / "oMLX" / "base-path"
    try:
        moved = bootstrap.read_text(encoding="utf-8").strip()
    except OSError:
        moved = ""
    base = Path(moved).expanduser() if moved else Path.home() / ".omlx"
    return base / "settings.json"


def launchd_jobs() -> dict[int, str] | None:
    """Running launchd jobs in this login session, PID to label; None when unreadable.

    ``launchctl list`` only reads.
    """
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                                timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    jobs: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            jobs[int(parts[0])] = parts[2].strip()
    return jobs


def _omlx_serve_processes() -> list[tuple[int, int]]:
    """``(pid, parent pid)`` of every ``omlx serve`` except CORSIA's managed one."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                                text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    from infra.config import STATE_ROOT

    managed_base = str(Path(STATE_ROOT) / MANAGED_DIR)
    found: list[tuple[int, int]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3 or "omlx serve" not in parts[2] or managed_base in parts[2]:
            continue
        try:
            found.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return found


def _job_label(jobs: dict[int, str] | None, pid: int, parent: int) -> str | None:
    """The launchd job running ``pid``, directly or as its launched child.

    Unreadable jobs answer None, so the process is treated as a plain one.
    """
    if not jobs:
        return None
    return jobs.get(pid) or (jobs.get(parent) if parent > 1 else None)


def foreign_omlx_process() -> int | None:
    """Return one positively identified non-CORSIA ``omlx serve`` PID, if any.

    This examines process commands only. It never reads a person's oMLX settings,
    registers their server, or treats an ambiguous process list as consent to stop.
    CORSIA's own v0.3/v0.4 Homebrew service is not foreign: the upgrade retires it.
    """
    processes = _omlx_serve_processes()
    if not processes:
        return None
    jobs = launchd_jobs()
    matches = [
        pid for pid, parent in processes
        if not (_job_label(jobs, pid, parent) == BREW_SERVICE_LABEL and legacy_setup_owns_current_omlx())
    ]
    return matches[0] if len(matches) == 1 else None


def stop_foreign_omlx_process(pid: int) -> bool:
    """Stop only the currently re-identified foreign oMLX process.

    A launchd job (``brew services`` runs one) would restart a signalled
    process, so its job is booted out of this login session instead. The
    person's plist is never edited, unloaded for good or deleted, and the job
    comes back at their next login. A plain process gets SIGTERM.
    """
    if foreign_omlx_process() != pid:
        return False
    parent = next((ppid for found, ppid in _omlx_serve_processes() if found == pid), 0)
    label = _job_label(launchd_jobs(), pid, parent)
    if label:
        try:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                           capture_output=True, text=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("could not stop the oMLX launchd job: %s", error)
            return False
        return True
    try:
        os.kill(pid, 15)
    except (OSError, ProcessLookupError):
        return False
    return True


def foreign_omlx_choice_path() -> Path:
    """The private state record for an explicit foreign-process choice."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / "foreign-omlx-choice.json"


def _remembered_foreign_omlx_choice(pid: int) -> bool | None:
    """Return Keep both for any later process; Stop mine only for its chosen PID."""
    try:
        value = json.loads(foreign_omlx_choice_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("stop"), bool):
        return None
    # Keep both is consent about the person's oMLX. Stop mine authorizes a
    # destructive action, so it must never survive a process identity change.
    if value["stop"] is False:
        return False
    return True if value.get("pid") == pid else None


def _remember_foreign_omlx_choice(pid: int, *, stop: bool) -> None:
    path = foreign_omlx_choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "stop": stop}), encoding="utf-8")


def apply_foreign_omlx_choice(*, consent_existing: bool, manage_existing: bool | None) -> str | None:
    """Apply the explicit or remembered choice about a foreign oMLX.

    Return a safe refusal code, or ``None`` when managed setup may proceed.
    Keep both survives a restart; stopping requires the originally chosen PID.
    """
    pid = foreign_omlx_process()
    if pid is None:
        return None
    stop = _remembered_foreign_omlx_choice(pid)
    # An explicit current choice supersedes a remembered Keep both or Stop mine.
    if consent_existing and manage_existing is not None:
        stop = manage_existing
        _remember_foreign_omlx_choice(pid, stop=stop)
    elif stop is None:
        if not consent_existing:
            return "consent_required"
        stop = False
        _remember_foreign_omlx_choice(pid, stop=stop)
    if stop and not stop_foreign_omlx_process(pid):
        return "foreign_omlx_changed"
    return None


def homebrew_failure_message(action: str, exc: OSError | subprocess.SubprocessError) -> str:
    """Return card-safe Homebrew repair feedback without exposing command arguments."""
    stderr = str(getattr(exc, "stderr", "") or "")
    detail = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    prefix = f"Homebrew couldn't {action} oMLX"
    return f"{prefix}: {detail}. Try again." if detail else f"{prefix}. Try again."


def ensure_omlx(progress: Callable[[str], None], *, needs_repair: bool = False,
                broken: bool = False) -> None:
    """Install or repair only the oMLX local setup owns through Homebrew."""
    if not installed_omlx():
        brew = shutil.which("brew")
        if not brew:
            raise RuntimeError("Homebrew is required to install oMLX. Install Homebrew, then retry.")
        progress("Installing oMLX")
        # The repository is not named homebrew-omlx, so the tap needs its URL.
        try:
            run_command([brew, "tap", "jundot/omlx", "https://github.com/jundot/omlx"])
            try:
                run_command([brew, "install", OMLX_FORMULA])
            except (OSError, subprocess.SubprocessError) as exc:
                # A leftover entry point not owned by Homebrew blocks the link step.
                if not command_output([brew, "list", "--versions", OMLX_FORMULA]):
                    raise exc
                run_command([brew, "link", "--overwrite", "omlx"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(homebrew_failure_message("install", exc)) from exc
        return
    brew = shutil.which("brew")
    if not needs_repair or not brew:
        return
    try:
        if not command_output([brew, "list", "--versions", OMLX_FORMULA]):
            return
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(homebrew_failure_message("check", exc)) from exc
    progress("Updating oMLX")
    try:
        # Explicit update refreshes taps even when HOMEBREW_NO_AUTO_UPDATE=1.
        run_command([brew, "tap", "jundot/omlx", "https://github.com/jundot/omlx"])
        run_command([brew, "update"])
        run_command([brew, "reinstall" if broken else "upgrade", OMLX_FORMULA])
    except (OSError, subprocess.SubprocessError) as exc:
        action = "reinstall" if broken else "update"
        raise RuntimeError(homebrew_failure_message(action, exc)) from exc


_LOG_ERROR = re.compile(r"^(ERROR|CRITICAL)\b| - (ERROR|CRITICAL) - |^\w*(Error|Exception):")


def omlx_log_detail() -> str:
    """oMLX's last error line from its Homebrew service log, as a clause to append, else ''."""
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
