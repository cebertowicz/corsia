"""Pure identity helpers for stock agent-browser daemon ownership."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys

import psutil


NAMESPACE_RE = re.compile(r"^corsia-[a-z0-9_-]+$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
SOCKET_PATH_LIMIT = 100
# Linux start times use whole-second btime, so a new daemon can read over 1 s old (clock sync adds more).
TIME_TOLERANCE_SECONDS = 2.0


class BrowserIdentityError(RuntimeError):
    """Browser daemon identity could not be safely established."""


class BrowserPidAbsent(Exception):
    """The exact captured PID no longer exists."""


class BrowserIdentityChanged(BrowserIdentityError):
    """A previously captured browser daemon identity changed."""


class BrowserExitTornRead(BrowserIdentityError):
    """An observation that may be torn during daemon exit."""


@dataclass(frozen=True, slots=True)
class BrowserSidecars:
    """Validated sidecar facts for one opaque agent-browser session."""

    pid: int
    pid_mtime: float
    version: str | None
    artifacts: tuple[tuple[int, int, int, int], ...] = ()


def creation_fingerprint(pid: int) -> str:
    process = process_for_identity(pid)
    create_time = process_create_time(process)
    return creation_fingerprint_material(pid, create_time)


def creation_fingerprint_material(pid: int, create_time: float) -> str:
    return hashlib.sha256(f"{pid}\0{create_time:.6f}".encode()).hexdigest()


def process_for_identity(pid: int) -> psutil.Process:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise BrowserIdentityError("browser daemon identity is invalid")
    try:
        return psutil.Process(pid)
    except psutil.NoSuchProcess as exc:
        raise BrowserPidAbsent from exc
    except (psutil.AccessDenied, OSError) as exc:
        raise BrowserIdentityError("browser daemon identity is unavailable") from exc


def process_create_time(process: psutil.Process) -> float:
    try:
        create_time = process.create_time()
    except psutil.NoSuchProcess as exc:
        raise BrowserPidAbsent from exc
    except (psutil.AccessDenied, OSError) as exc:
        raise BrowserIdentityError("browser daemon identity is unavailable") from exc
    if not math.isfinite(create_time):
        raise BrowserIdentityError("browser daemon identity is unavailable")
    return create_time


def executable_fingerprint_for_path(path: Path) -> tuple[str, str]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise BrowserIdentityError("browser daemon executable identity is unavailable") from exc
    material = (
        f"{resolved}\0{metadata.st_dev}\0{metadata.st_ino}\0"
        f"{metadata.st_mode}\0{metadata.st_size}\0{metadata.st_mtime_ns}"
    )
    return str(resolved), hashlib.sha256(material.encode()).hexdigest()


def executable_fingerprint_for_process(
    process: psutil.Process,
) -> tuple[str, str]:
    try:
        executable = process.exe()
    except psutil.NoSuchProcess as exc:
        raise BrowserPidAbsent from exc
    except (psutil.AccessDenied, OSError) as exc:
        raise BrowserIdentityError("browser daemon executable identity is unavailable") from exc
    if not executable:
        raise BrowserIdentityError("browser daemon executable identity is unavailable")
    return executable_fingerprint_for_path(Path(executable))


def is_zombie(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() in {
            psutil.STATUS_ZOMBIE,
            getattr(psutil, "STATUS_DEAD", "dead"),
            "terminated",
        }
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


def default_socket_root() -> Path:
    return Path("/tmp")


def ensure_private_dir(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise BrowserIdentityError("browser socket directory is unavailable") from exc
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise BrowserIdentityError("browser socket directory is not private")
    return path.resolve(strict=True)


def socket_dir_for_namespace(
    namespace: str,
    instance_key: str | None,
) -> Path:
    """Choose a short private socket path for this owner's backend lifetime."""
    owner_seed = instance_key or namespace
    digest = hashlib.sha256(
        f"{owner_seed}\0{namespace}".encode("utf-8")
    ).hexdigest()[:16]
    socket_dir = default_socket_root() / f"{digest}-{secrets.token_hex(12)}"
    if len(str(socket_dir / ("s" * 32 + ".sock"))) > SOCKET_PATH_LIMIT:
        raise ValueError("browser socket directory path is too long")
    return socket_dir


def artifact_stamp(path: Path, *, socket_file: bool = False) -> tuple[int, int, int, int]:
    """Identify an exact artifact without following links or opening a FIFO."""
    try:
        metadata = path.lstat()
        valid = socket_artifact_mode(metadata.st_mode) if socket_file else stat.S_ISREG(metadata.st_mode)
        if not valid:
            raise ValueError
        return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size
    except (OSError, ValueError) as exc:
        error_type = BrowserExitTornRead if isinstance(exc, FileNotFoundError) else BrowserIdentityError
        raise error_type("browser session artifact is unavailable") from exc


def socket_artifact_mode(mode: int) -> bool:
    """Require a Unix socket, not a symlink or a regular-file substitute."""
    return stat.S_ISSOCK(mode)


def session_absent(socket_dir: Path, session: str) -> bool:
    """Prove absence of the exact native runtime artifact set, including dangling links."""
    for suffix in ("pid", "version", "sock", "port", "stream", "engine", "provider", "extensions"):
        try:
            session_sidecar_path(socket_dir, session, suffix).lstat()
            return False
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BrowserIdentityError("browser session absence is unavailable") from exc
    return True


def browser_process_identity(
    pid: int,
    *,
    sidecar_mtime: float,
    admitted_at: float,
    attempt_started_at: float | None,
    session: str,
    socket_dir: Path,
    expected_executable: str,
    supported_version: str,
    version: str | None,
    time_tolerance_seconds: float,
) -> tuple[str, str]:
    if version != supported_version:
        raise BrowserIdentityError("browser daemon version could not be verified")
    process = process_for_identity(pid)
    if is_zombie(pid):
        raise BrowserIdentityError("browser daemon identity is unavailable")
    create_time = process_create_time(process)
    if create_time + time_tolerance_seconds < admitted_at:
        raise BrowserIdentityError("browser daemon identity is stale")
    if (
        attempt_started_at is not None
        and (sidecar_mtime + time_tolerance_seconds < attempt_started_at
             or create_time + time_tolerance_seconds < attempt_started_at)
    ):
        raise BrowserIdentityError("browser daemon identity is stale")
    if create_time > sidecar_mtime + time_tolerance_seconds:
        raise BrowserIdentityError("browser daemon identity is stale")
    _executable, executable_fingerprint = executable_fingerprint_for_process(process)
    if executable_fingerprint != expected_executable:
        raise BrowserIdentityError("browser daemon executable identity changed")
    if not daemon_environment_matches(
        process,
        session=session,
        socket_dir=socket_dir,
    ):
        raise BrowserIdentityError("browser daemon environment identity changed")
    observed_again = creation_fingerprint(pid)
    fingerprint = creation_fingerprint_material(pid, create_time)
    if observed_again != fingerprint:
        raise BrowserIdentityChanged("browser daemon identity changed")
    return fingerprint, executable_fingerprint


def parse_pid_sidecar(raw_pid: str) -> int:
    stripped = raw_pid.strip()
    if not stripped or not stripped.isdecimal():
        raise BrowserIdentityError("browser session ownership could not be verified")
    pid = int(stripped)
    if pid <= 1:
        raise BrowserIdentityError("browser session ownership could not be verified")
    return pid


def session_sidecar_path(socket_dir: Path, session: str, suffix: str) -> Path:
    if SESSION_RE.fullmatch(session) is None:
        raise BrowserIdentityError("browser session ownership could not be verified")
    path = socket_dir / f"{session}.{suffix}"
    try:
        path.parent.resolve(strict=True).relative_to(socket_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise BrowserIdentityError("browser session ownership could not be verified") from exc
    return path


def _read_regular_sidecar(path: Path, limit: int) -> tuple[str, float]:
    if path.is_symlink():
        raise BrowserIdentityError("browser session ownership could not be verified")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise BrowserExitTornRead("browser session ownership could not be verified") from exc
    except OSError as exc:
        raise BrowserIdentityError("browser session ownership could not be verified") from exc
    try:
        sidecar_stat = os.fstat(fd)
        if not stat.S_ISREG(sidecar_stat.st_mode):
            raise BrowserIdentityError(
                "browser session ownership could not be verified"
            )
        raw = os.read(fd, limit).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BrowserIdentityError("browser session ownership could not be verified") from exc
    finally:
        os.close(fd)
    return raw, sidecar_stat.st_mtime


def read_sidecars(socket_dir: Path, session: str) -> BrowserSidecars:
    before = tuple(artifact_stamp(session_sidecar_path(socket_dir, session, suffix),
                                  socket_file=suffix == "sock")
                   for suffix in ("pid", "version", "sock"))
    pid_raw, pid_mtime = _read_regular_sidecar(
        session_sidecar_path(socket_dir, session, "pid"),
        64,
    )
    version_path = session_sidecar_path(socket_dir, session, "version")
    try:
        version_raw, _version_mtime = _read_regular_sidecar(version_path, 64)
        version = version_raw.strip() or None
    except BrowserIdentityError:
        version = None
    artifacts = tuple(artifact_stamp(session_sidecar_path(socket_dir, session, suffix),
                                     socket_file=suffix == "sock")
                      for suffix in ("pid", "version", "sock"))
    if before != artifacts:
        raise BrowserIdentityChanged("browser session artifacts changed during observation")
    return BrowserSidecars(pid=parse_pid_sidecar(pid_raw), pid_mtime=pid_mtime,
                           version=version, artifacts=artifacts)


def daemon_environment_matches(
    process: psutil.Process,
    *,
    session: str,
    socket_dir: Path,
) -> bool:
    try:
        env = process.environ()
    except psutil.NoSuchProcess as exc:
        raise BrowserPidAbsent from exc
    except psutil.AccessDenied as exc:
        raise BrowserExitTornRead("browser daemon environment identity is unavailable") from exc
    except OSError as exc:
        raise BrowserIdentityError("browser daemon environment identity is unavailable") from exc
    return (
        env.get("AGENT_BROWSER_DAEMON") == "1"
        and env.get("AGENT_BROWSER_SESSION") == session
        and env.get("AGENT_BROWSER_SOCKET_DIR") == str(socket_dir)
    )


@dataclass(frozen=True, slots=True)
class DaemonBinding:
    """One immutable native process and its exact session locator."""
    pid: int
    birth: str
    executable: str
    sidecars: BrowserSidecars


@dataclass(frozen=True, slots=True)
class Observation:
    """Current facts, not a claim that unobserved descendants have disappeared."""
    binding: DaemonBinding | None
    alive: bool
    artifacts_absent: bool


def observe_session(*, socket_dir: Path, session: str, binding: DaemonBinding | None,
                    admitted_at: float, attempt_started_at: float | None,
                    executable: str, version: str, peer_socket: socket.socket | None = None) -> Observation:
    """Apply one identity policy for command admission, cleanup and retry.

    Run off the event loop. A bound process may exit or remove its artifacts while
    producer close completes; these are observations, never closure certification.
    """
    absent = session_absent(socket_dir, session)
    if binding is None and absent:
        return Observation(None, False, True)
    if binding is None and attempt_started_at is None:
        raise BrowserIdentityError("browser daemon launch was not verified")
    sidecars = binding.sidecars if absent and binding is not None else read_sidecars(socket_dir, session)
    if binding is not None and sidecars != binding.sidecars:
        raise BrowserIdentityChanged("browser daemon locator changed")
    if is_zombie(sidecars.pid):
        return Observation(binding, False, absent)
    try:
        birth, observed_executable = browser_process_identity(
            sidecars.pid, sidecar_mtime=sidecars.pid_mtime, admitted_at=admitted_at,
            attempt_started_at=attempt_started_at if binding is None else None,
            session=session, socket_dir=socket_dir, expected_executable=executable,
            supported_version=version, version=sidecars.version, time_tolerance_seconds=TIME_TOLERANCE_SECONDS,
        )
    except (BrowserIdentityError, BrowserPidAbsent):
        # macOS can deny environ() while the bound process is exiting. A fresh
        # kernel disappearance is observable; the owner still requires close ack.
        if binding is not None and is_zombie(binding.pid):
            return Observation(binding, False, session_absent(socket_dir, session))
        raise
    observed = DaemonBinding(sidecars.pid, birth, observed_executable, sidecars)
    if peer_socket is not None and connected_peer_pid(peer_socket) != observed.pid:
        raise BrowserIdentityChanged("browser connected peer identity changed")
    if binding is not None and binding != observed:
        raise BrowserIdentityChanged("browser daemon identity changed")
    if not absent and read_sidecars(socket_dir, session) != sidecars:
        raise BrowserIdentityChanged("browser session artifacts changed during observation")
    return Observation(observed, True, absent)


def require_exit_torn_read(failure: BaseException) -> None:
    """Fail unless an acknowledged exit can cause this specific missing read."""
    if not isinstance(failure.__cause__, (BrowserPidAbsent, BrowserExitTornRead)):
        raise failure


def connected_peer_pid(peer_socket: socket.socket) -> int:
    """Read the connected Unix peer from the kernel, never from its current pathname."""
    try:
        if sys.platform == "darwin":
            # sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2 (not exposed by Python).
            return struct.unpack("i", peer_socket.getsockopt(0, 2, 4))[0]
        if hasattr(socket, "SO_PEERCRED"):
            return struct.unpack("3i", peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    except (OSError, ValueError, struct.error) as exc:
        raise BrowserIdentityError("browser connected peer identity is unavailable") from exc
    raise BrowserIdentityError("browser connected peer identity is unsupported")
