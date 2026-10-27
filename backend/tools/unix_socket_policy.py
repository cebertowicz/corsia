"""AF_UNIX endpoint policy for protected shell and Safehouse grants."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
import sys
import tempfile

from host_access.browser_identity import (
    BrowserIdentityError,
    SOCKET_PATH_LIMIT,
    ensure_private_dir,
)
from infra.config import WORKSPACE
from tools.host_path_policy import GENERATED_WORKSPACE_PROJECTIONS


_TEMP_ROOT_NAME = "corsia-af-unix"
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A full UUID thread id under macOS's per-user $TMPDIR overruns the socket
# path limit, so each thread's root is named by a short digest of its id.
_THREAD_ROOT_NAME_LENGTH = 12

logger = logging.getLogger(__name__)


class UnixSocketPathLimitError(RuntimeError):
    """An AF_UNIX socket root is too long for the platform limit."""


class UnixSocketRootError(RuntimeError):
    """An AF_UNIX socket root cannot be safely granted."""


_BIND_ROOT_WITHHELD_REASONS: dict[str, str] = {}


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _validate_socket_path(root: Path, *, message: str) -> None:
    if len(os.fsencode(root / "socket.sock")) >= SOCKET_PATH_LIMIT:
        raise UnixSocketPathLimitError(message)


def corsia_temp_root() -> Path:
    """Create and validate CORSIA's private, short AF_UNIX endpoint root."""
    try:
        root = ensure_private_dir(
            _resolved(Path(tempfile.gettempdir())) / _TEMP_ROOT_NAME
        )
    except BrowserIdentityError as exc:
        raise UnixSocketRootError(
            "CORSIA AF_UNIX temp root is unavailable or not private"
        ) from exc
    _validate_socket_path(
        root,
        message="CORSIA AF_UNIX temp root exceeds the macOS socket path limit",
    )
    return root


def _thread_root_name(thread_id: str) -> str:
    digest = hashlib.sha256(thread_id.encode()).hexdigest()
    return digest[:_THREAD_ROOT_NAME_LENGTH]


def corsia_temp_root_for_bind(thread_id: str) -> Path:
    """Create the private AF_UNIX bind root for one already-authenticated thread."""
    if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
        # An identity breach is not a root failure: it stops the shell.
        raise RuntimeError("CORSIA AF_UNIX bind root requires a valid thread identity")
    try:
        if sys.platform == 'linux':
            from infra.config import STATE_ROOT
            from tools.linux_sandbox import thread_tmp_root
            root = thread_tmp_root(STATE_ROOT, thread_id)
            root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            return ensure_private_dir(root)
        root = ensure_private_dir(corsia_temp_root() / _thread_root_name(thread_id))
    except BrowserIdentityError as exc:
        raise UnixSocketRootError(
            "CORSIA AF_UNIX thread root is unavailable or not private"
        ) from exc
    _validate_socket_path(
        root,
        message="CORSIA AF_UNIX thread root exceeds the macOS socket path limit",
    )
    return root


def _withhold_bind_root(thread_id: str, exc: RuntimeError) -> list[Path]:
    reason = str(exc)
    if _BIND_ROOT_WITHHELD_REASONS.get(thread_id) != reason:
        level = (
            logging.WARNING
            if isinstance(exc, UnixSocketPathLimitError)
            else logging.ERROR
        )
        logger.log(level, "AF_UNIX temp-root grant withheld for %s: %s", thread_id, reason)
    _BIND_ROOT_WITHHELD_REASONS[thread_id] = reason
    return []


def thread_bind_root_withheld_reason(thread_id: str) -> str | None:
    """Return the reason profile construction withheld this thread's bind grant."""
    return _BIND_ROOT_WITHHELD_REASONS.get(thread_id)


def thread_bind_roots(thread_id: str | None) -> list[Path]:
    """Return the thread's bind root as a grant list, empty when unusable.

    An unusable root withholds only the temp-root AF_UNIX grant; the shell
    keeps its other grants instead of failing every call.
    """
    if thread_id is None:
        return []
    if sys.platform == 'linux':
        if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
            raise RuntimeError("CORSIA AF_UNIX bind root requires a valid thread identity")
        return []  # The same root is mounted at /tmp by the Linux renderer.
    try:
        root = corsia_temp_root_for_bind(thread_id)
    except (UnixSocketPathLimitError, UnixSocketRootError) as exc:
        return _withhold_bind_root(thread_id, exc)
    _BIND_ROOT_WITHHELD_REASONS.pop(thread_id, None)
    return [root]


def get_unix_socket_allowed_roots(thread_id: str | None = None) -> list[Path]:
    """Return roots ordinary agent shells may use for AF_UNIX sockets.

    The temporary bind root is granted only when the protected shell supplies
    its authenticated thread identity; the shared parent is never a grant.
    """
    return [_resolved(WORKSPACE), *thread_bind_roots(thread_id)]


def get_unix_socket_denied_roots() -> list[Path]:
    """Return generated workspace projections that never host agent sockets."""
    workspace = _resolved(WORKSPACE)
    return [workspace / relative for relative in GENERATED_WORKSPACE_PROJECTIONS]


def _sandbox_path(path: Path) -> str:
    return str(_resolved(path)).replace("\\", "\\\\").replace('"', '\\"')


def _unix_socket_rules(action: str, root: Path) -> list[str]:
    path = _sandbox_path(root)
    return [
        f'({action} network-bind (local unix-socket (subpath "{path}")))',
        f'({action} network-outbound (remote unix-socket (subpath "{path}")))',
    ]


def unix_socket_sandbox_rules(thread_id: str | None = None) -> str:
    """Render Safehouse AF_UNIX bind and connect rules scoped to the shell's thread."""
    lines = [";; CORSIA AF_UNIX SOCKET BOUNDARY"]
    for root in get_unix_socket_allowed_roots(thread_id):
        lines.extend(_unix_socket_rules("allow", root))
    for root in get_unix_socket_denied_roots():
        lines.extend(_unix_socket_rules("deny", root))
    return "\n".join(lines)
