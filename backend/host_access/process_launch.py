"""Pre-exec admission gate and identity binding for owned processes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import signal
import stat
import sys
from typing import Any, Callable, Sequence

import psutil

LAUNCH_GATE_REFUSED_EXIT = 125
_LAUNCH_GATE_CODE = (
    "import os,sys;"
    "fd=int(sys.argv[1]);"
    "token=os.read(fd,1);"
    "os.close(fd);"
    f"sys.exit({LAUNCH_GATE_REFUSED_EXIT}) if token!=b'G' else "
    "os.execv(sys.argv[2],sys.argv[2:])"
)


_FCHDIR_EXEC_CODE = (
    "import os,sys;"
    "fd=int(sys.argv[1]);"
    "os.fchdir(fd);"
    "os.close(fd);"
    "os.execv(sys.argv[2],sys.argv[2:])"
)


@dataclass(frozen=True, slots=True)
class SpawnSpec:
    argv: tuple[str, ...]
    cwd: str | None
    options: dict[str, Any]
    cleanup_paths: tuple[Path, ...]


def prepare_spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    cwd_fd: int | None,
    cleanup_paths: Sequence[Path],
    error_type: type[Exception],
) -> SpawnSpec:
    """Validate and freeze the filesystem-bound part of one process launch."""
    if not argv or not all(isinstance(part, str) and part for part in argv):
        raise error_type("owned process argv is invalid")
    owned_cleanup_paths = tuple(
        dict.fromkeys(
            Path(path).expanduser().resolve(strict=False)
            for path in cleanup_paths
        )
    )
    launch_argv = tuple(argv)
    launch_cwd: str | None = str(Path(cwd).expanduser().resolve(strict=False))
    options: dict[str, Any] = {}
    if cwd_fd is not None:
        if isinstance(cwd_fd, bool) or not isinstance(cwd_fd, int) or cwd_fd < 0:
            raise error_type("owned process cwd descriptor is invalid")
        try:
            cwd_status = os.fstat(cwd_fd)
        except OSError as exc:
            raise error_type("owned process cwd descriptor is unavailable") from exc
        if not stat.S_ISDIR(cwd_status.st_mode):
            raise error_type("owned process cwd descriptor is not a directory")
        launch_argv = (
            sys.executable,
            "-I",
            "-c",
            _FCHDIR_EXEC_CODE,
            str(cwd_fd),
            *launch_argv,
        )
        launch_cwd = None
        options["pass_fds"] = (cwd_fd,)
    return SpawnSpec(launch_argv, launch_cwd, options, owned_cleanup_paths)


def gated_launch_spec(
    argv: Sequence[str], spawn_options: dict[str, Any],
) -> tuple[tuple[str, ...], dict[str, Any], int, int]:
    """Wrap final argv behind an inherited one-byte gate."""
    read_fd, write_fd = os.pipe()
    inherited = tuple(spawn_options.get("pass_fds", ()))
    options = {**spawn_options, "pass_fds": (*inherited, read_fd)}
    gated = (
        sys.executable,
        "-I",
        "-c",
        _LAUNCH_GATE_CODE,
        str(read_fd),
        *tuple(argv),
    )
    return gated, options, read_fd, write_fd


def close_fd(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


async def bind_process_launch(
    process: asyncio.subprocess.Process,
    *,
    handle: str,
    pid: int,
    log_path: Path,
    fingerprint: Callable[[int], str],
) -> str:
    """Bind a live child, preserving the identity of one that already exited."""
    from host_access.processes import ProcessBindingError

    try:
        return fingerprint(pid)
    except ProcessBindingError as error:
        if isinstance(error.__cause__, psutil.NoSuchProcess):
            await process.wait()
            material = f"exited-before-bind\0{handle}\0{pid}"
            return hashlib.sha256(material.encode()).hexdigest()
        await discard_refused_launch(process, pid=pid, log_path=log_path)
        raise
    except BaseException:
        await discard_refused_launch(process, pid=pid, log_path=log_path)
        raise


async def discard_refused_launch(
    process: asyncio.subprocess.Process,
    *,
    pid: int,
    log_path: Path,
) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        await process.wait()
        log_path.unlink(missing_ok=True)
