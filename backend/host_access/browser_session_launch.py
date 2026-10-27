"""Which saved sign-in a managed browser session launches with, and the copy a concurrent one starts from.

Chrome locks a profile to one running browser. The first session of an owner holds the shared profile; a
session that starts while it is held launches on a throwaway profile seeded with a copy of the holder's
sign-in (agent-browser's ``state save`` / ``--state``), kept in private state and removed when it closes.
A session whose owning user cannot be resolved receives neither the profile nor a copy.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from host_access import browser_cli, browser_identity as identity, browser_launch

logger = logging.getLogger(__name__)
_STATE_SAVE_TIMEOUT = 30.0


async def resolve_profile(thread_id: str) -> str | None:
    """Return the saved profile for a resolved owner, or use an ephemeral browser."""
    profile = await asyncio.to_thread(browser_launch.profile_dir, thread_id)
    return str(profile) if profile is not None else None


def assign(sessions: Mapping[str, Any], profile: str | None, thread_id: str) -> tuple[str | None, str | None]:
    """Transfer an idle reservation, or copy from a holder that has started launching."""
    if profile is None:
        return None, None
    holder = next((runtime for runtime in sessions.values() if runtime.record.profile == profile), None)
    if holder is None:
        return profile, None
    if holder.record.binding is None and not holder.command_lock.locked() and holder.attempt is None:
        previous = holder.record.thread_id
        holder.record = replace(holder.record, profile=None, copy_of=thread_id)
        for waiting in sessions.values():
            if (waiting is not holder and waiting.record.copy_of == previous and waiting.attempt is None
                    and not waiting.command_lock.locked()):
                waiting.record = replace(waiting.record, copy_of=thread_id)
        return profile, None
    return None, holder.record.thread_id


def flags(record: Any, ua: str | None) -> Sequence[str]:
    """Launch-only flags for a session's first command."""
    copy = browser_launch.copy_path(record.session) if record.copy_of else None
    return browser_launch.launch_argv(
        ua, headless=not record.show_window,
        profile=Path(record.profile) if record.profile else None, state_copy=copy,
    )


def prepare_directories() -> None:
    """Make the profile root private, and drop copies a crashed backend left behind."""
    root = browser_launch.profile_root()
    copies = root / browser_launch.COPIES_DIR_NAME
    copies.mkdir(mode=0o700, parents=True, exist_ok=True)
    for folder in (root, copies):
        os.chmod(folder, 0o700)
    for stale in copies.iterdir():
        with contextlib.suppress(OSError):
            stale.unlink()


async def seed_copy(owner: Any, runtime: Any) -> None:
    """Save the holder's sign-in for this session's first launch; a miss leaves it signed out."""
    record = runtime.record
    holder = owner._sessions.get(record.copy_of)
    if holder is None:
        return
    target = browser_launch.copy_path(record.session)
    try:
        async with holder.command_lock:
            if holder.record.binding is None or holder.record.terminal_state != "running":
                return
            result = await owner._invoke(
                owner._session_argv(holder, ("state", "save", str(target))), timeout=_STATE_SAVE_TIMEOUT)
        if result.returncode == 0 and target.is_file():
            os.chmod(target, 0o600)
        else:
            logger.warning("browser sign-in copy was not saved: exit=%s", result.returncode)
    except Exception:
        logger.warning("browser sign-in copy failed", exc_info=True)


def drop_copy(record: Any) -> None:
    """Delete this session's copy of the sign-in once it is gone."""
    if record.copy_of:
        with contextlib.suppress(OSError):
            browser_launch.copy_path(record.session).unlink()


async def select_chrome(owner: Any, agent_browser: str) -> tuple[str | None, str]:
    """The Chrome agent-browser launches; with none installed, fetch agent-browser's own first."""
    chrome, source = await asyncio.to_thread(browser_cli.chrome_executable)
    if source == "default" and await asyncio.to_thread(browser_cli.chrome_download_missing):
        # No Chrome on this Mac: agent-browser's own install fetches Chrome for Testing
        # into the person's home, with no admin step.
        install = await owner._invoke((agent_browser, "install"), timeout=browser_cli.CHROME_INSTALL_TIMEOUT)
        if install.completion != "exited" or install.timed_out or install.returncode:
            logger.warning("agent-browser install failed: completion=%s timed_out=%s exit=%s stderr=%s",
                           install.completion, install.timed_out, install.returncode, (install.stderr or "")[-500:])
            raise identity.BrowserIdentityError(
                "agent-browser could not download its browser (Chrome for Testing); "
                "try again, and tell the person if it keeps failing")
    return chrome, source
