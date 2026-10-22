"""The tools an agent's shell finds first on a Mac with no developer tools.

CORSIA's bundled Python, then Node/npm/npx, uv/uvx and gh from the pinned runtime
folder. The folder is read-only to shells, so a missing runtime is fetched here, by
the backend, before the shell starts; installs and caches land in places a shell may write.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile

from infra.platform_paths import app_bundle_root
from tools.managed_runtime import (
    COMMAND_RUNTIME, ManagedRuntimeError, ensure_runtime, runtime_bin_dir, runtime_cache_root, runtime_is_installed,
)

logger = logging.getLogger(__name__)
# The commands CORSIA manages are managed_runtime's: the same set the connected-app launcher fetches for.
_MANAGED = "|".join(sorted(COMMAND_RUNTIME, key=len, reverse=True))
# A managed command used as a command: at the start of the line or after ; & | ( or `, past any VAR=value.
_COMMAND_USED = re.compile(rf"(?:^|[;&|(\n`])\s*(?:\w+=\S*\s+)*({_MANAGED})(?=[\s;&|)`]|$)")
_NOT_FOUND = re.compile(rf"(?:^|[\s:])({_MANAGED}): (?:command )?not found")
# Environment names a shell child needs from CORSIA, passed through the sandbox by name.
ENV_NAMES = ("npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR")


def _bundled_python_dir() -> Path | None:
    bundle = app_bundle_root()
    directory = None if bundle is None else bundle / "Contents" / "Resources" / "python" / "bin"
    return directory if directory is not None and (directory / "python3").is_file() else None


def readable_dirs() -> list[Path]:
    """The folders the PATH entries live in, read-only inside the sandbox: the app's Python and the runtime cache."""
    python_dir = _bundled_python_dir()
    candidates = [None if python_dir is None else python_dir.parent, runtime_cache_root()]
    return [directory for directory in candidates if directory is not None and directory.is_dir()]


def agent_cache_dir(thread_id: str | None) -> Path:
    """One thread's package cache: writable by its shells, shared with no other thread, outside the runtime folder and home."""
    return Path(tempfile.gettempdir()) / "corsia-agent-cache" / re.sub(r"[^\w.-]", "_", thread_id or "none")


def shell_environment(path: str, thread_id: str | None) -> dict[str, str]:
    """PATH with CORSIA's tools first, and the caches that keep installs in writable places."""
    python_dir = _bundled_python_dir()
    entries = [str(python_dir)] if python_dir is not None else []
    entries += [str(directory) for kind in ("node", "uv", "gh") if (directory := runtime_bin_dir(kind)) is not None]
    cache = agent_cache_dir(thread_id)
    env = {
        "PATH": os.pathsep.join(dict.fromkeys([*entries, *(part for part in path.split(os.pathsep) if part)])),
        "npm_config_cache": str(cache / "npm"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "UV_CACHE_DIR": str(cache / "uv"),
    }
    return env


def _missing(commands: set[str]) -> set[str]:
    try:
        # A tool the person already has is theirs to run: only a command found nowhere is fetched.
        return {COMMAND_RUNTIME[command] for command in commands
                if not shutil.which(command) and not runtime_is_installed(COMMAND_RUNTIME[command])}
    except ManagedRuntimeError:
        return set()  # No pinned runtime for this Mac: the command resolves from the person's PATH or not at all.


async def prepare_for_command(command: str) -> str | None:
    """Fetch the pinned runtimes a command names that are not installed yet; an error text, or None."""
    return await _fetch({match[1] for match in _COMMAND_USED.finditer(command)})


async def noted_output(owned, parts: tuple) -> tuple:
    """A shell's output parts, with the install note appended when a managed command was missing."""
    output_parts, stdout_text, stderr_text = parts
    if owned.returncode == 127 and (note := await note_after_not_found(stdout_text + stderr_text)):
        output_parts.append("\n" + note)
    return output_parts, stdout_text, stderr_text


async def note_after_not_found(output: str) -> str | None:
    """A managed command a script ran by itself was missing: fetch it and say to run it again."""
    names = {match[1] for match in _NOT_FOUND.finditer(output)}
    if not _missing(names):
        return None
    return await _fetch(names) or "CORSIA has now installed the tool that was missing. Run the command again."


async def _fetch(commands: set[str]) -> str | None:
    for kind in sorted(_missing(commands)):
        try:
            await asyncio.to_thread(ensure_runtime, kind)
        except ManagedRuntimeError as exc:
            logger.warning("could not install the %s runtime for an agent shell: %s", kind, exc)
            return (f"Error: CORSIA could not install {kind}, which this command needs, so it did not run. "
                    "Do not retry in a loop; tell the person if it keeps failing.")
    return None
