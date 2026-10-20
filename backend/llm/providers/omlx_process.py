"""Process and filesystem helpers for CORSIA-managed oMLX servers."""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import psutil

from llm.managed_omlx_binary import managed_omlx_executable


def pid_path(runtime_dir: Path, server_id: str) -> Path:
    return runtime_dir / f"omlx-{server_id}.pid"


def log_path(runtime_dir: Path, server_id: str) -> Path:
    return runtime_dir / f"omlx-{server_id}.log"


def read_pid(runtime_dir: Path, server_id: str, *, legacy_port_owner_pid: int | None = None,
             preserve_legacy: bool = False) -> int | None:
    """Return a live spawn record, adopting only a legacy PID that owns its row's port."""
    try:
        record = json.loads(pid_path(runtime_dir, server_id).read_text())
        if type(record) is int:
            if preserve_legacy:
                return None
            if record > 0 and legacy_port_owner_pid == record:
                write_pid(runtime_dir, server_id, record)
                return read_pid(runtime_dir, server_id)
            clear_pid(runtime_dir, server_id)
            return None
        pid = record["pid"]
        if type(pid) is int and pid > 0:
            proc = psutil.Process(pid)
            if (proc.create_time() == record["create_time"] and proc.is_running()
                    and proc.status() != psutil.STATUS_ZOMBIE):
                return pid
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        pass
    clear_pid(runtime_dir, server_id)
    return None


def write_pid(runtime_dir: Path, server_id: str, pid: int) -> None:
    """Bind the supervisor's spawn record to this process's creation time."""
    try:
        created = psutil.Process(pid).create_time()
    except psutil.Error:
        clear_pid(runtime_dir, server_id)
        return
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pid_path(runtime_dir, server_id).write_text(json.dumps({"pid": pid, "create_time": created}))


def clear_pid(runtime_dir: Path, server_id: str) -> None:
    try:
        pid_path(runtime_dir, server_id).unlink()
    except FileNotFoundError:
        pass


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def port_owner_pid(host: str, port: int) -> int | None:
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        out = subprocess.run(
            [lsof, "-nPiTCP:" + str(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def port_is_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            return False
        return False
    finally:
        sock.close()


def build_argv(
    host: str,
    port: int,
    settings: dict,
    *,
    executable: str | None = None,
) -> list[str]:
    """Build argv for CORSIA's managed oMLX server."""
    executable = executable or managed_omlx_executable() or "omlx"
    argv: list[str] = [executable, "serve", "--host", host, "--port", str(port)]

    flag_map: list[tuple[str, str]] = [
        ("model_dir",                "--model-dir"),
        ("max_model_memory",         "--max-model-memory"),
        ("max_process_memory",       "--max-process-memory"),
        ("log_level",                "--log-level"),
        ("max_concurrent_requests",  "--max-concurrent-requests"),
        ("paged_ssd_cache_dir",      "--paged-ssd-cache-dir"),
        ("paged_ssd_cache_max_size", "--paged-ssd-cache-max-size"),
        ("hot_cache_max_size",       "--hot-cache-max-size"),
        ("initial_cache_blocks",     "--initial-cache-blocks"),
        ("base_path",                "--base-path"),
        ("api_key",                  "--api-key"),
    ]
    for key, flag in flag_map:
        val = settings.get(key)
        if val is None or val == "":
            continue
        argv.extend([flag, str(val)])

    if settings.get("no_cache"):
        argv.append("--no-cache")

    return argv


def tail_log(runtime_dir: Path, server_id: str, lines: int = 20) -> str:
    path = log_path(runtime_dir, server_id)
    if not path.exists():
        return ""
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - 16 * 1024))
            data = file.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""
