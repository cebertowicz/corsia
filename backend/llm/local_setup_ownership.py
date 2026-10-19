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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MARKER_NAME = "local_setup_installed_omlx"
OMLX_FORMULA = "jundot/omlx/omlx"
_MANAGED_DIR = "managed-omlx"
_MANAGED_PORTS = range(8210, 8310)

log = logging.getLogger(__name__)


def marker_path() -> Path:
    """Legacy v0.4 ownership marker, retained only to migrate CORSIA's old service."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / MARKER_NAME


@dataclass(frozen=True)
class ManagedOmlxPaths:
    """Filesystem and port that belong exclusively to CORSIA's managed oMLX."""

    base_path: Path
    model_dir: Path
    settings_path: Path
    port: int


def managed_omlx_paths() -> ManagedOmlxPaths:
    """Return CORSIA-owned oMLX paths and a durable private loopback port.

    The base and model paths live under the state root. A first launch chooses a
    free port, then writes it in this private settings file so retries and
    restarts address the same managed process.
    """
    from infra.config import STATE_ROOT
    from llm.providers.omlx_process import port_is_free

    base = Path(STATE_ROOT) / _MANAGED_DIR
    models = base / "models"
    settings = base / "settings.json"
    try:
        raw = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    configured = server.get("port")
    if isinstance(configured, int) and configured in _MANAGED_PORTS:
        port = configured
    else:
        port = next((candidate for candidate in _MANAGED_PORTS if port_is_free(candidate)), None)
        if port is None:
            raise RuntimeError("CORSIA couldn't reserve a private port for its local model server. Try again.")
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    raw["server"] = {**server, "port": port}
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    raw["model"] = {**model, "model_dir": str(models)}
    settings.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return ManagedOmlxPaths(base, models, settings, port)


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
    brew, omlx = shutil.which("brew"), installed_omlx()
    if not marker_path().exists() or not brew or not omlx:
        return False
    try:
        return setup_owns_current_omlx(brew, omlx)
    except (OSError, subprocess.SubprocessError):
        return False


def retire_legacy_owned_omlx_service() -> bool:
    """Stop only the prior service that the v0.3/v0.4 proof identifies as ours.

    A failure leaves the private managed launch safe: its port and paths are
    distinct, and the old instance remains excluded from CORSIA's catalog.
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


def is_corsia_managed_server(server: dict, metadata: dict | None = None) -> bool:
    """Whether a network row is exactly CORSIA's private managed oMLX process.

    Rows from v0.3/v0.4 named an oMLX install without proving its process or
    files were CORSIA's. They are deliberately excluded; setup replaces them
    with this state-root identity after it launches the supervised subprocess.
    """
    from infra.config import STATE_ROOT

    if (
        server.get("provider") != "omlx"
        or server.get("kind") != "managed"
        or server.get("instance_id") != "corsia-managed"
    ):
        return False
    if metadata is None:
        raw = server.get("metadata_json")
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            return False
    if not isinstance(metadata, dict) or metadata.get("corsia_managed") is not True:
        return False
    base = Path(STATE_ROOT) / _MANAGED_DIR
    return (
        metadata.get("base_path") == str(base)
        and metadata.get("model_dir") == str(base / "models")
    )


def legacy_omlx_settings_file() -> Path:
    """Read the old CORSIA service settings only after ownership is proven."""
    return Path.home() / ".omlx" / "settings.json"


def foreign_omlx_process() -> int | None:
    """Return one positively identified non-CORSIA ``omlx serve`` PID, if any.

    This examines process commands only. It never reads a person's oMLX settings,
    registers their server, or treats an ambiguous process list as consent to stop.
    """
    try:
        result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    from infra.config import STATE_ROOT

    managed_base = str(Path(STATE_ROOT) / _MANAGED_DIR)
    matches: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or "omlx serve" not in parts[1] or managed_base in parts[1]:
            continue
        try:
            matches.append(int(parts[0]))
        except ValueError:
            continue
    return matches[0] if len(matches) == 1 else None


def stop_foreign_omlx_process(pid: int) -> bool:
    """Terminate only the currently re-identified foreign oMLX process."""
    if foreign_omlx_process() != pid:
        return False
    try:
        os.kill(pid, 15)
    except (OSError, ProcessLookupError):
        return False
    return True


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


def download_complete(folder: Path, download_bytes: int) -> bool:
    """Whether ``folder`` holds a finished download: every expected byte, and no partial file.

    A cancelled or failed download leaves its folder behind (oMLX resumes there), so the
    folder existing proves nothing. Partial files carry Hugging Face's ``.incomplete``
    suffix until they are renamed into place.
    """
    if not folder.is_dir():
        return False
    for _root, _dirs, files in os.walk(folder):
        if any(name.endswith(".incomplete") for name in files):
            return False
    return _present_bytes(folder) >= download_bytes


def download_disk_space(download_bytes: int, model_dir: Path | None, repository: str) -> dict[str, int | str]:
    """Describe the actual remaining download space requirement without guessing.

    oMLX resumes inside ``<model_dir>/<repository>``, so present partial bytes are
    credited and the existing 10% safety margin applies only to the remainder.
    """
    if model_dir is None:
        return {"state": "unknown"}
    path = model_dir
    try:
        while not path.exists() and path != path.parent:
            path = path.parent
        free = shutil.disk_usage(path).free
        remaining = max(0, download_bytes - _present_bytes(model_dir / repository))
    except OSError:
        return {"state": "unknown"}
    required = int(remaining * 1.1)
    return {
        "state": "ready" if free >= required else "insufficient",
        "free_bytes": free,
        "remaining_bytes": remaining,
        "required_bytes": required,
    }


def require_free_disk(download_bytes: int, model_dir: Path | None, repository: str) -> None:
    """Raise DiskSpaceError when oMLX's download volume lacks room for the remainder."""
    space = download_disk_space(download_bytes, model_dir, repository)
    if space["state"] == "unknown":
        log.warning("Free disk space check skipped: oMLX download directory is unavailable")
        return
    free = int(space["free_bytes"])
    needed = int(space["required_bytes"])
    if space["state"] == "insufficient":
        short = (needed - free) / 1000**3
        raise DiskSpaceError(
            f"This download needs {needed / 1000**3:.0f} GB of free disk space and this computer has "
            f"{free / 1000**3:.0f} GB. Free up about {short:.0f} GB, then run local setup again.")



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
