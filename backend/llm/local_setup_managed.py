"""Private state-root identity and storage for CORSIA's managed oMLX."""

from __future__ import annotations

import json
import psutil
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MARKER_NAME = "local_setup_installed_omlx"
BREW_SERVICE_LABEL = "homebrew.mxcl.omlx"

MANAGED_DIR = "managed-omlx"
MANAGED_INSTANCE_ID = "corsia-managed"
MANAGED_PORTS = range(8210, 8310)


@dataclass(frozen=True)
class ManagedOmlxPaths:
    """Filesystem locations that belong exclusively to CORSIA's managed oMLX."""

    base_path: Path
    model_dir: Path
    settings_path: Path


class ManagedPortUnavailable(RuntimeError):
    """No port in CORSIA's private range can be bound for its oMLX right now."""


def managed_omlx_paths() -> ManagedOmlxPaths:
    """Return the durable private paths for CORSIA's oMLX.

    A pure lookup: it creates, binds and writes nothing, so any reader (startup
    included) may call it. ``reserve_managed_omlx_port`` prepares a launch.
    """
    from infra.config import STATE_ROOT

    base = Path(STATE_ROOT) / MANAGED_DIR
    return ManagedOmlxPaths(base, base / "models", base / "settings.json")


def reserve_managed_omlx_port() -> tuple[ManagedOmlxPaths, int]:
    """Create the private directories and keep a loopback port for a launch.

    Only starting the managed server calls this. A port kept in its settings
    stays; otherwise the first bindable port in ``MANAGED_PORTS`` is recorded.
    Raises ``ManagedPortUnavailable`` when none can be bound.
    """
    from llm.providers.omlx_process import port_is_free

    paths = managed_omlx_paths()
    base, models, settings = paths.base_path, paths.model_dir, paths.settings_path
    try:
        raw = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    server_value = raw.get("server")
    server = server_value if isinstance(server_value, dict) else {}
    configured = server.get("port")
    if isinstance(configured, int) and configured in MANAGED_PORTS:
        port = configured
    else:
        free_port = next((candidate for candidate in MANAGED_PORTS if port_is_free(candidate)), None)
        if free_port is None:
            raise ManagedPortUnavailable(
                "CORSIA couldn't reserve a private port for its local model server. Try again.")
        port = free_port
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    raw["server"] = {**server, "port": port}
    model_value = raw.get("model")
    model = model_value if isinstance(model_value, dict) else {}
    raw["model"] = {**model, "model_dir": str(models)}
    settings.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return paths, port


def marker_path() -> Path:
    """Return the proof record written when setup installs its Homebrew binary."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / MARKER_NAME


def mark_installed_by_setup() -> None:
    """Record a fresh install without making it look like a pending legacy upgrade."""
    from llm.local_setup_upgrade import done_path

    marker_path().parent.mkdir(parents=True, exist_ok=True)
    done_path().write_text("fresh managed install\n", encoding="utf-8")
    marker_path().write_text("installed by corsia local setup\n", encoding="utf-8")


def setup_owns_current_omlx(brew: str, omlx: str) -> bool:
    """Prove both the install record and the executable's current Homebrew keg."""
    from llm.local_setup_ownership import OMLX_FORMULA, command_output

    if not marker_path().exists() or not command_output([brew, "list", "--versions", OMLX_FORMULA]):
        return False
    prefix = command_output([brew, "--prefix", OMLX_FORMULA])
    return bool(prefix) and Path(omlx).resolve().is_relative_to(Path(prefix).resolve())


def legacy_setup_owns_current_omlx() -> bool:
    """Whether the prior Homebrew executable is positively identified as CORSIA's."""
    from llm.local_setup_ownership import installed_omlx

    if not marker_path().exists():
        return False
    brew, omlx = shutil.which("brew"), installed_omlx()
    if not brew or not omlx:
        return False
    try:
        return setup_owns_current_omlx(brew, omlx)
    except (OSError, subprocess.SubprocessError):
        return False


def adoption_record_paths() -> tuple[Path, Path, Path]:
    """The dev adoption originals, read-consent marker and use-mine marker, in order."""
    from infra.config import STATE_ROOT

    root = Path(STATE_ROOT)
    return (root / "own-omlx-management.json", root / "local_setup_existing_omlx_consented",
            root / "own-omlx-use-mine")


def classify_omlx_server(server: dict, metadata: dict | None = None) -> str:
    """Classify managed, legacy, retired adoption, and personal rows without probes.

    Only the older setup fingerprint (manual ``This Mac`` on its recorded
    loopback port) retires. Hand-added, remote and External rows are preserved.
    Retirement separately preserves legacy rows until their upgrade succeeds.
    """
    from llm.local_setup_runtime import LOCAL_SERVER_NAME
    from llm.local_setup_upgrade import legacy_omlx_port

    if _managed_server(server, metadata):
        return "managed"
    if (server.get("provider") != "omlx" or server.get("kind") == "external"
            or server.get("source") != "manual" or server.get("instance_name") != LOCAL_SERVER_NAME):
        return "personal"
    instance_id = str(server.get("instance_id") or "")
    if not instance_id.startswith("127.0.0.1:"):
        return "personal"
    if marker_path().exists() and instance_id == f"127.0.0.1:{legacy_omlx_port()}":
        return "legacy"
    originals, _consent, use_mine = adoption_record_paths()
    return "retired" if originals.exists() or use_mine.exists() else "personal"


def is_retired_local_omlx(server: dict) -> bool:
    """Whether an older setup/adoption row can retire without losing a pending upgrade."""
    from llm.local_setup_upgrade import pending_legacy_port

    owner = classify_omlx_server(server)
    return owner == "retired" or (owner == "legacy" and pending_legacy_port() is None)


def is_corsia_managed_server(server: dict, metadata: dict | None = None) -> bool:
    """Whether the canonical row classifier identifies CORSIA's private server."""
    return classify_omlx_server(server, metadata) == "managed"


def _managed_server(server: dict, metadata: dict | None = None) -> bool:
    if (
        server.get("provider") != "omlx"
        or server.get("kind") != "managed"
        or server.get("instance_id") != MANAGED_INSTANCE_ID
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
    paths = managed_omlx_paths()
    return (
        metadata.get("base_path") == str(paths.base_path)
        and metadata.get("model_dir") == str(paths.model_dir)
    )


@dataclass(frozen=True)
class OmlxProcess:
    """One positively identified oMLX server and its launchd job, if known."""

    pid: int
    parent: int
    label: str | None
    owner: Literal["managed", "legacy", "personal"]


def launchd_jobs() -> dict[int, str] | None:
    """Read this login's PID-to-label map; None means launchd could not be inspected."""
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                                timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    jobs = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            jobs[int(parts[0])] = parts[2].strip()
    return jobs


def _serve_argv(argv: list[str]) -> list[str] | None:
    if not argv:
        return None
    executable = Path(argv[0]).name
    index = 1 if executable.lower().startswith("python") else 0
    if len(argv) > index + 1 and Path(argv[index]).name == "omlx" and argv[index + 1] == "serve":
        return argv[index + 2:]
    return None


def _managed_argv(argv: list[str]) -> bool:
    value = ""
    for index, token in enumerate(argv):
        if token == "--base-path":
            value = argv[index + 1] if index + 1 < len(argv) else ""
        elif token.startswith("--base-path="):
            value = token.partition("=")[2]
        else:
            continue
    return bool(value) and Path(value).expanduser().is_absolute() and (
        Path(value).expanduser().resolve() == managed_omlx_paths().base_path.resolve()
    )


def omlx_processes() -> tuple[OmlxProcess, ...] | None:
    """Inspect all server argv; None is unknown, distinct from a known empty tuple.

    Only an exact --base-path value proves the managed process. The brew job
    belongs to CORSIA only while its positively owned legacy upgrade is pending.
    """
    from llm.local_setup_upgrade import pending_legacy_port

    parsed = []
    try:
        for process in psutil.process_iter(["pid", "name", "ppid", "cmdline"]):
            info = process.info
            cmdline = info.get("cmdline")
            if cmdline is None:
                name = str(info.get("name") or "").lower()
                if name == "omlx" or name.startswith("python"):
                    return None
                continue
            argv = _serve_argv(cmdline)
            if argv is not None:
                parsed.append((info["pid"], info["ppid"], argv))
    except (OSError, psutil.Error):
        return None
    if not parsed:
        return ()
    jobs = launchd_jobs() or {}
    legacy_pending = pending_legacy_port() is not None
    found = []
    for pid, parent, argv in parsed:
        label = jobs.get(pid) or (jobs.get(parent) if parent > 1 else None)
        owner: Literal["managed", "legacy", "personal"] = "personal"
        if _managed_argv(argv):
            owner = "managed"
        elif label == BREW_SERVICE_LABEL and legacy_pending:
            owner = "legacy"
        found.append(OmlxProcess(pid, parent, label, owner))
    return tuple(found)


def foreign_omlx_processes() -> tuple[OmlxProcess, ...] | None:
    """Every personal oMLX process, or None when process inspection failed."""
    processes = omlx_processes()
    return None if processes is None else tuple(p for p in processes if p.owner == "personal")
