"""The tools an agent's shell finds on a Mac with no developer tools.

The person's own tools come first on PATH. CORSIA's bundled Python sits just before
/usr/bin (past the person's Homebrew or pyenv, ahead of the Command Line Tools stub that
opens an install dialog), and its pinned Node, uv and gh come last. The pinned folder is
read-only to shells, so the packaged app's backend fetches a missing one once, at startup,
through managed_runtime; installs and caches land in places a shell may write.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import tempfile

from infra.platform_paths import app_bundle_root
from tools.managed_runtime import (
    COMMAND_RUNTIME, RUNTIME_PINS, ManagedRuntimeError, ensure_runtime, runtime_bin_dir, runtime_cache_root,
    runtime_fetch_plan,
)

logger = logging.getLogger(__name__)
# Environment names a shell child needs from CORSIA, passed through the sandbox by name.
ENV_NAMES = ("npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR")
# The bundled Git's helper commands and templates, which Git finds relative to its own binary.
_GIT_RUNTIME_DIRS = ("libexec/git-core", "share/git-core")
_prefetch: asyncio.Task | None = None


def _bundle_resources() -> Path | None:
    bundle = app_bundle_root()
    return None if bundle is None else bundle / "Contents" / "Resources"


def _bundled_python_dir() -> Path | None:
    resources = _bundle_resources()
    directory = None if resources is None else resources / "python" / "bin"
    return directory if directory is not None and (directory / "python3").is_file() else None


def readable_dirs() -> list[Path]:
    """The folders the PATH entries live in, read-only inside the sandbox.

    The app's Python, its Git runtime (the exec path with git-remote-https, and the templates
    init and clone copy) and the runtime cache; each only where it exists.
    """
    python_dir = _bundled_python_dir()
    resources = _bundle_resources()
    candidates = [
        None if python_dir is None else python_dir.parent,
        *(None if resources is None else resources / name for name in _GIT_RUNTIME_DIRS),
        runtime_cache_root(),
    ]
    return [directory for directory in candidates if directory is not None and directory.is_dir()]


def agent_cache_dir(thread_id: str | None) -> Path:
    """One thread's package cache: writable by its shells, shared with no other thread, outside the runtime folder and home."""
    return Path(tempfile.gettempdir()) / "corsia-agent-cache" / re.sub(r"[^\w.-]", "_", thread_id or "none")


def _path_entries(path: str) -> list[str]:
    parts = [part for part in path.split(os.pathsep) if part]
    if (python_dir := _bundled_python_dir()) is not None:
        parts.insert(parts.index("/usr/bin") if "/usr/bin" in parts else len(parts), str(python_dir))
    return [*parts, *(str(directory) for kind in RUNTIME_PINS if (directory := runtime_bin_dir(kind)) is not None)]


def shell_environment(path: str, thread_id: str | None) -> dict[str, str]:
    """The person's PATH with CORSIA's tools around it, and the caches that keep installs in writable places."""
    cache = agent_cache_dir(thread_id)
    return {
        "PATH": os.pathsep.join(dict.fromkeys(_path_entries(path))),
        "npm_config_cache": str(cache / "npm"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "UV_CACHE_DIR": str(cache / "uv"),
    }


def prefetch_task() -> asyncio.Task | None:
    return _prefetch


def start_prefetch() -> None:
    """Packaged app only: begin fetching, once, the pinned runtimes the person's PATH lacks."""
    global _prefetch
    if app_bundle_root() is not None and _prefetch is None:
        _prefetch = asyncio.get_running_loop().create_task(_fetch_missing())


async def wait_for_prefetch() -> None:
    """A shell waits here before it spawns; it costs nothing once the fetch has finished."""
    if _prefetch is not None:
        await asyncio.shield(_prefetch)


async def _fetch_missing() -> None:
    for kind in RUNTIME_PINS:
        try:
            # runtime_fetch_plan is the test the connected-app launcher uses: a command on the
            # person's PATH, or an installed pin, needs nothing.
            if any(runtime_fetch_plan(command) for command, owner in COMMAND_RUNTIME.items() if owner == kind):
                await asyncio.to_thread(ensure_runtime, kind)
        except (ManagedRuntimeError, OSError) as exc:
            # Shells run anyway, and a tool still missing says so itself. No retry loop.
            logger.warning("could not install the %s runtime for agent shells: %s", kind, exc)
