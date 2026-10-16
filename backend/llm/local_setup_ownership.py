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

MARKER_NAME = "local_setup_installed_omlx"
CONSENT_MARKER_NAME = "local_setup_existing_omlx_consented"
OMLX_FORMULA = "jundot/omlx/omlx"
DEFAULT_PORT = 8000

log = logging.getLogger(__name__)


def marker_path() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / MARKER_NAME


def consent_marker_path() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / CONSENT_MARKER_NAME


def mark_installed_by_setup() -> None:
    """Record, durably, that local setup itself installed oMLX."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("installed by corsia local setup\n", encoding="utf-8")


def record_existing_omlx_consent(*, manage: bool | None = None) -> None:
    """Remember consent to inspect the pre-existing oMLX in this CORSIA state root.

    ``manage`` is the person's choice between the two (``own_omlx_management``):
    True is "Let CORSIA manage it", also consent to read that oMLX's admin key and
    change the settings shown first; False is "Use mine", where the key is never
    read and nothing is offered or written. None is a consent given where those
    choices weren't shown, which records neither.
    """
    path = consent_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("consented to existing oMLX\n", encoding="utf-8")
    if manage is None:
        return
    from llm import own_omlx_management

    if manage:
        own_omlx_management.consent()
    else:
        own_omlx_management.choose_use_mine()


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


def existing_omlx_requires_consent() -> bool:
    """Whether setup must ask before inspecting a pre-existing oMLX.

    This deliberately checks only PATH and CORSIA's ownership/consent markers.
    In particular, it must not execute oMLX before the person has consented;
    a broken leftover follows the same consent journey and is repaired after
    that consent by local setup.
    """
    return bool(
        shutil.which("omlx")
        and not marker_path().exists()
        and not consent_marker_path().exists()
    )


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
    """Whether the marked oMLX executable is inside the installed formula's keg.

    A ``brew`` call that fails raises ``OSError`` or ``SubprocessError``: the
    answer is unknown, and each caller decides what unknown means for it.
    """
    if not command_output([brew, "list", "--versions", OMLX_FORMULA]):
        return False
    prefix = command_output([brew, "--prefix", OMLX_FORMULA])
    return bool(prefix) and Path(omlx).resolve().is_relative_to(Path(prefix).resolve())


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
        mark_installed_by_setup()
        return
    brew = shutil.which("brew")
    omlx = installed_omlx()
    if not needs_repair or not marker_path().exists() or not brew or not omlx:
        return
    try:
        if not setup_owns_current_omlx(brew, omlx):
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


_foreign_cache: dict[tuple, bool] = {}


def existing_install_is_foreign() -> bool:
    """True unless the marked PATH oMLX resolves under setup's installed formula.

    Two ``brew`` calls decide it, so the answer is cached per marker mtime and
    resolved ``omlx`` path: a reinstall or a moved binary re-checks. A failed
    ``brew`` call reads as foreign for that read only and is not cached.
    """
    omlx = installed_omlx()
    if not omlx or not marker_path().exists():
        return omlx is not None
    brew = shutil.which("brew")
    if not brew:
        return True
    key = (marker_path().stat().st_mtime_ns, str(Path(omlx).resolve()), brew)
    # Read once: another thread may clear the cache between a check and an index.
    foreign = _foreign_cache.get(key)
    if foreign is None:
        try:
            foreign = not setup_owns_current_omlx(brew, omlx)
        except (OSError, subprocess.SubprocessError):
            return True
        _foreign_cache.clear()
        _foreign_cache[key] = foreign
    return foreign


def own_omlx_settings_file() -> Path:
    """Where the person's own oMLX keeps ``settings.json``.

    oMLX's macOS app writes a one-line ``base-path`` file when the person moves
    its data root, and the file lives there; otherwise it is under ``~/.omlx``
    (oMLX v0.7.0 ``omlx/settings.py``: bootstrap file 46-49, resolution 52-71,
    ``settings.json`` joined to the base path at 1076). An ``OMLX_BASE_PATH``
    environment variable wins in oMLX but is that process's own, so not visible here.
    """
    bootstrap = Path.home() / "Library" / "Application Support" / "oMLX" / "base-path"
    try:
        moved = bootstrap.read_text(encoding="utf-8").strip()
    except OSError:
        moved = ""
    base = Path(moved).expanduser() if moved else Path.home() / ".omlx"
    return base / "settings.json"


class DiskSpaceError(RuntimeError):
    """The models volume lacks room for the download; the message says how much to free."""


def _present_bytes(folder: Path) -> int:
    """Bytes already under ``folder`` (finished and partial files); 0 when it is absent."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def require_free_disk(download_bytes: int, model_dir: Path | None, repository: str) -> None:
    """Raise DiskSpaceError when ``model_dir``'s volume lacks room for what is still to download.

    ``model_dir`` is the directory oMLX reports it downloads to. oMLX writes into
    ``<model_dir>/<repository>`` and resumes there, skipping files it already holds
    (oMLX ``omlx/admin/hf_downloader.py``, ``retry_download`` and ``start_download``), so the
    bytes already present are credited and the 10% margin applies to the remainder only.
    The nearest existing parent stands in until oMLX has created the directory. With no
    ``model_dir``, or when free space can't be read, the check is skipped with a warning
    (oMLX reports a full disk itself).
    """
    if model_dir is None:
        log.warning("Free disk space check skipped: oMLX reported no model directory")
        return
    path = model_dir
    try:
        while not path.exists() and path != path.parent:
            path = path.parent
        free = shutil.disk_usage(path).free
        remaining = max(0, download_bytes - _present_bytes(model_dir / repository))
    except OSError as exc:
        log.warning("Free disk space check skipped for %s: %s", path, exc)
        return
    needed = int(remaining * 1.1)
    if free < needed:
        short = (needed - free) / 1000**3
        raise DiskSpaceError(
            f"This download needs {needed / 1000**3:.0f} GB of free disk space and this Mac has "
            f"{free / 1000**3:.0f} GB. Free up about {short:.0f} GB, then run local setup again.")


def own_omlx_port(settings_file: Path | None = None) -> int:
    """The port the person's own oMLX serves on: its settings file, else 8000.

    oMLX keeps it at ``server.port`` of that file (v0.7.0 ``omlx/settings.py``:
    ``ServerSettings.port`` default 8000 at 190-194, read from the file's
    ``server`` section at 1110) and a ``--port`` flag or ``OMLX_PORT`` can override
    it for one launch (``omlx/cli.py:499``, ``omlx/settings.py:1174``), which this
    cannot see. Only that one field is read; nothing else in the file (it also
    holds the server's api_key) is kept, logged or returned, and nothing is written.
    """
    port = _settings_section(settings_file or own_omlx_settings_file(), "server").get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return port
    return DEFAULT_PORT


def own_omlx_admin_key(settings_file: Path | None = None) -> str | None:
    """The admin key oMLX already holds, from its settings file, else None.

    A settings file left by the person's earlier oMLX keeps that oMLX's key, and
    admin sign-in accepts only it (v0.7.0 ``omlx/admin/routes.py`` ``login``,
    1906-1939; ``AuthSettings.api_key`` at ``omlx/settings.py:649``), so setup
    signs in with it rather than a new one. An ``OMLX_API_KEY`` set for one launch
    (``omlx/settings.py:1269``) is not visible here. Only ``auth.api_key`` is read;
    it goes to the caller alone, is never logged, and nothing is written.
    """
    key = _settings_section(settings_file or own_omlx_settings_file(), "auth").get("api_key")
    return key if isinstance(key, str) and key else None


def _settings_section(path: Path, name: str) -> dict:
    """One top-level section of oMLX's settings file, or {} when it cannot be read."""
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get(name)
    except (OSError, ValueError, AttributeError):
        return {}
    return section if isinstance(section, dict) else {}


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
