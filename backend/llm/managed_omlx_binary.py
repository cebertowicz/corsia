"""Canonical identity, executable and private paths for CORSIA's managed oMLX."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

from infra.platform_paths import app_bundle_root

MARKER_NAME = "managed-omlx-installed"
OMLX_FORMULA = "jundot/omlx/omlx"
MANAGED_DIR = "managed-omlx"
MANAGED_INSTANCE_ID = "corsia-managed"
MANAGED_PORTS = range(8210, 8310)


def managed_omlx_executable(path: str | Path | None = None) -> str | None:
    """Return the bundled executable by absolute path, or None for a source install."""
    bundle = app_bundle_root(path)
    if bundle is None:
        return None
    executable = bundle / "Contents" / "Resources" / "bin" / "omlx"
    if not executable.is_file():
        raise RuntimeError("The CORSIA app is damaged: its bundled oMLX executable is missing.")
    return str(executable)


def is_managed_omlx_executable(path: str | Path) -> bool:
    """Whether a path resolves to the executable shipped in this app bundle."""
    try:
        executable = managed_omlx_executable()
        return executable is not None and Path(path).resolve() == Path(executable).resolve()
    except (RuntimeError, OSError):
        return False


def marker_path() -> Path:
    """The proof record for binaries installed by managed setup."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / MARKER_NAME


def mark_installed_by_setup() -> None:
    """Record a new managed install without claiming any prior installation."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("installed by corsia managed setup\n", encoding="utf-8")


def command_output(command: list[str]) -> str:
    """Run a bounded binary-ownership inspection."""
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def setup_owns_current_omlx(brew: str, omlx: str) -> bool:
    """Prove both the managed-install record and the executable's current brew keg."""
    if not marker_path().exists() or not command_output([brew, "list", "--versions", OMLX_FORMULA]):
        return False
    prefix = command_output([brew, "--prefix", OMLX_FORMULA])
    return bool(prefix) and Path(omlx).resolve().is_relative_to(Path(prefix).resolve())


@dataclass(frozen=True)
class ManagedOmlxPaths:
    """Filesystem locations belonging exclusively to the managed server."""
    base_path: Path
    model_dir: Path
    settings_path: Path


class ManagedPortUnavailable(RuntimeError):
    """No private port can currently be bound."""


def managed_omlx_paths() -> ManagedOmlxPaths:
    """Look up private paths without creating directories or probing the host."""
    from infra.config import STATE_ROOT

    base = Path(STATE_ROOT) / MANAGED_DIR
    return ManagedOmlxPaths(base, base / "models", base / "settings.json")


def reserve_managed_omlx_port() -> tuple[ManagedOmlxPaths, int]:
    """Prepare private directories and record a port for the managed launch."""
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
            raise ManagedPortUnavailable("CORSIA couldn't reserve a private port for its local model server. Try again.")
        port = free_port
    base.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    raw["server"] = {**server, "port": port}
    model_value = raw.get("model")
    model = model_value if isinstance(model_value, dict) else {}
    raw["model"] = {**model, "model_dir": str(models)}
    settings.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    return paths, port


def is_corsia_managed_server(server: dict, metadata: dict | None = None) -> bool:
    """Identify only a private managed row, without process, filesystem or brew probes."""
    if (server.get("provider") != "omlx" or server.get("kind") != "managed"
            or server.get("instance_id") != MANAGED_INSTANCE_ID):
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
    return metadata.get("base_path") == str(paths.base_path) and metadata.get("model_dir") == str(paths.model_dir)


@dataclass(frozen=True)
class OmlxProcess:
    """One oMLX server; its label names a job only if that job's PID is this PID."""
    pid: int
    parent: int
    label: str | None
    owner: Literal["managed", "personal"]


def launchd_jobs() -> dict[int, str] | None:
    """Read this login's PID-to-label map, or None when inspection is unavailable."""
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5, check=True)
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
    # oMLX rewrites its serving title, dropping the arguments that prove ownership.
    if argv[0] == "omlx-server":
        return argv[1:]
    index = 1 if Path(argv[0]).name.lower().startswith("python") else 0
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
    return bool(value) and Path(value).expanduser().is_absolute() and (
        Path(value).expanduser().resolve() == managed_omlx_paths().base_path.resolve()
    )


def omlx_processes() -> tuple[OmlxProcess, ...] | None:
    """Inspect same-user servers; exact private base-path proves managed ownership."""
    parsed = []
    current_uid = os.getuid()
    try:
        for process in psutil.process_iter(["pid", "name", "ppid", "cmdline", "status", "uids"]):
            info = process.info
            if info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            if getattr(info.get("uids"), "real", None) != current_uid:
                continue
            cmdline = info.get("cmdline")
            if not cmdline:
                name = str(info.get("name") or "").lower()
                if name in {"omlx", "omlx-server"} or name.startswith("python"):
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
    return tuple(OmlxProcess(pid, parent, jobs.get(pid), "managed" if _managed_argv(argv) else "personal")
                 for pid, parent, argv in parsed)


def foreign_omlx_processes() -> tuple[OmlxProcess, ...] | None:
    """All personal oMLX processes, or None if inspection failed."""
    processes = omlx_processes()
    return None if processes is None else tuple(p for p in processes if p.owner == "personal")
