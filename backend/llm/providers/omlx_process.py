"""Process and filesystem helpers for CORSIA-managed oMLX servers."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import select
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from llm.managed_omlx_binary import managed_omlx_executable, managed_omlx_install_error, omlx_command


log = logging.getLogger("llm.providers.omlx.process")


class _PortOwnerUnknown:
    """Sentinel for a port inspection that could not establish ownership."""


PORT_OWNER_UNKNOWN = _PortOwnerUnknown()
PortOwnerPid = int | None | _PortOwnerUnknown


def process_exited(pid: int, alive: Callable[[int], bool]) -> bool:
    """Reap an exited child, or check the identity of a process adopted from another parent."""
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return not alive(pid)
    return waited_pid == pid or not alive(pid)


def pid_path(runtime_dir: Path, server_id: str) -> Path:
    return runtime_dir / f"omlx-{server_id}.pid"


def log_path(runtime_dir: Path, server_id: str) -> Path:
    return runtime_dir / f"omlx-{server_id}.log"


def read_pid(runtime_dir: Path, server_id: str, *, legacy_port_owner_pid: PortOwnerPid = PORT_OWNER_UNKNOWN,
             preserve_legacy: bool = False) -> int | None:
    """Return a live spawn record, preserving a legacy one unless a probe proves a mismatch."""
    try:
        record = json.loads(pid_path(runtime_dir, server_id).read_text())
        if type(record) is int:
            if preserve_legacy:
                return None
            if record > 0 and legacy_port_owner_pid == record:
                write_pid(runtime_dir, server_id, record)
                return read_pid(runtime_dir, server_id)
            if isinstance(legacy_port_owner_pid, int) and legacy_port_owner_pid != record:
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


def is_recorded_process(runtime_dir: Path, pid: int) -> bool:
    """Match a process to a durable spawn record, preserving legacy records during identity reads."""
    return any(read_pid(path.parent, path.stem.removeprefix("omlx-"), preserve_legacy=True) == pid
               for path in runtime_dir.glob("omlx-*.pid"))


def write_pid(runtime_dir: Path, server_id: str, pid: int, *, omlx_version: str | None = None) -> None:
    """Bind the supervisor's spawn record to this process's creation time and, for a managed server, its build."""
    try:
        created = psutil.Process(pid).create_time()
    except psutil.Error:
        clear_pid(runtime_dir, server_id)
        return
    runtime_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, int | float | str] = {"pid": pid, "create_time": created}
    if omlx_version is not None:
        record["omlx"] = omlx_version
    pid_path(runtime_dir, server_id).write_text(json.dumps(record))


def recorded_omlx_version(runtime_dir: Path, server_id: str) -> str | None:
    """The build recorded when this server was spawned; None when the record is older than that field or gone."""
    try:
        record = json.loads(pid_path(runtime_dir, server_id).read_text())
    except (OSError, ValueError):
        return None
    version = record.get("omlx") if isinstance(record, dict) else None
    return version if isinstance(version, str) else None


def clear_pid(runtime_dir: Path, server_id: str) -> None:
    try:
        pid_path(runtime_dir, server_id).unlink()
    except FileNotFoundError:
        pass


def watch_exit(pid: int, on_exit: Callable[[tuple[str, bool] | None], None]) -> Callable[[], None] | None:
    """Report the OS exit status for a child or an adopted process, without polling.

    macOS supplies NOTE_EXITSTATUS for children and processes we can signal. Reap a child after the
    exit event before notifying the supervisor, so it cannot mistake a zombie for a running server.
    """
    kq: Any = select  # kqueue names exist on macOS/BSD only; Linux type checkers cannot see them
    if not hasattr(kq, "kqueue"):
        return None
    loop = asyncio.get_running_loop()
    queue = kq.kqueue()
    exit_status_flag = 0x04000000  # Darwin NOTE_EXITSTATUS; Python's select does not expose it
    event = kq.kevent(pid, kq.KQ_FILTER_PROC, kq.KQ_EV_ADD | kq.KQ_EV_ONESHOT,
                      kq.KQ_NOTE_EXIT | exit_status_flag)
    try:
        queue.control([event], 0, 0)
    except ProcessLookupError:
        queue.close()
        loop.call_soon(on_exit, reap_status(pid, True))  # it was already gone when we asked
        return lambda: None
    except OSError as exc:
        queue.close()
        log.warning("oMLX exit watch for pid %s could not start: %s", pid, exc)
        return None

    def cancel() -> None:
        if not queue.closed:
            loop.remove_reader(queue.fileno())
            queue.close()

    def fired() -> None:
        events = queue.control(None, 1, 0)
        if not events:
            return
        cancel()
        raw = events[0].data if events[0].fflags & exit_status_flag else None
        reaped = reap_status(pid, True)
        on_exit(_status_from_raw(raw) if raw is not None else reaped)

    loop.add_reader(queue.fileno(), fired)
    return cancel


def reap_status(pid: int, wait: bool = False) -> tuple[str, bool]:
    """Reap a child and report its status; adopted processes have no waitable status."""
    try:
        waited, raw = os.waitpid(pid, 0 if wait else os.WNOHANG)
    except ChildProcessError:
        return "unknown (not our child)", False
    if waited != pid:
        return "unknown", False
    return _status_from_raw(raw)


def _status_from_raw(raw: int) -> tuple[str, bool]:
    """Decode the wait status shared by waitpid and Darwin's exit event."""
    code = os.waitstatus_to_exitcode(raw)
    return (f"signal {-code}" if code < 0 else f"exit code {code}"), code == 0


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


def port_owner_pid(host: str, port: int) -> PortOwnerPid:
    """Return a listening PID, no owner, or unknown when the probe is unreadable."""
    lsof = shutil.which("lsof")
    if not lsof:
        return PORT_OWNER_UNKNOWN
    try:
        out = subprocess.run(
            [lsof, "-nPiTCP:" + str(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return PORT_OWNER_UNKNOWN
    if out.returncode != 0:
        # lsof uses exit 1 with no output for its ordinary "no matching
        # listener" result. Confirm that the port can bind before treating it
        # as unowned: an unreadable or raced probe must remain unknown.
        if out.returncode == 1 and not out.stdout and not out.stderr and port_is_free(port):
            return None
        return PORT_OWNER_UNKNOWN
    if not out.stdout:
        return None
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                continue
    return PORT_OWNER_UNKNOWN


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


def spawn_process(argv: list[str], log_path: Path) -> subprocess.Popen | dict[str, Any]:
    """Spawn one supervised process, returning a logged failure if launch fails."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_fh = log_path.open("ab", buffering=0)
        return subprocess.Popen(
            argv, env={**os.environ, "OMLX_SUPERVISED": "corsia"},
            stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except Exception as exc:
        log.exception("Failed to spawn oMLX")
        return {"ok": False, "message": "Couldn’t start the model server. Try again.", "error": str(exc)}


def managed_omlx_argv(host: str, port: int, settings: dict, *, managed: bool) -> list[str] | dict[str, Any]:
    """Resolve the executable and build arguments for an oMLX server."""
    if managed:
        install_error = managed_omlx_install_error()
        if install_error:
            return {"ok": False, "message": install_error}
        executable = managed_omlx_executable()
    else:
        executable = None
    executable = executable or shutil.which("omlx")
    if not executable:
        return {
            "ok": False,
            "message": (
                "omlx binary not found on PATH. Install oMLX "
                "(`brew install omlx` or pip install omlx) and retry."
            ),
        }
    return build_argv(host, port, settings, executable=executable)


def build_argv(
    host: str,
    port: int,
    settings: dict,
    *,
    executable: str | None = None,
) -> list[str]:
    """Build argv for CORSIA's managed oMLX server."""
    executable = executable or managed_omlx_executable() or "omlx"
    argv: list[str] = [*omlx_command(executable), "serve", "--host", host, "--port", str(port)]

    flag_map: list[tuple[str, str]] = [
        ("model_dir",                "--model-dir"),
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
        if key == "api_key":
            # A generated key can start with '-', which argparse would read
            # as an option; the joined form always binds it to the flag.
            argv.append(f"{flag}={val}")
            continue
        argv.extend([flag, str(val)])

    if settings.get("no_cache"):
        argv.append("--no-cache")

    return argv


def log_size(runtime_dir: Path, server_id: str) -> int | None:
    try:
        return log_path(runtime_dir, server_id).stat().st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def logged_since(runtime_dir: Path, server_id: str, offset: int | None, text: str) -> bool | None:
    """Whether ``text`` was logged after ``offset``; None if the offset or log is unreadable."""
    if offset is None:
        return None
    try:
        with log_path(runtime_dir, server_id).open("rb") as file:
            size = file.seek(0, os.SEEK_END)
            file.seek(max(offset, size - 64 * 1024))
            return text.encode() in file.read()
    except OSError:
        return None


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
