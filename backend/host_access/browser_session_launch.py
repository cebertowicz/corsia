"""Which saved sign-in a managed browser session launches with, and the copy a concurrent one starts from.

Chrome locks a profile to one running browser. The first session of an owner holds the shared profile; a
session that starts while it is held launches on a throwaway profile seeded with a copy of the holder's
sign-in (agent-browser's ``state save`` / ``--state``), kept in private state and removed when it closes.
An untrusted session never touches the shared profile or a copy of it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from host_access import browser_cli, browser_identity as identity, browser_launch

logger = logging.getLogger(__name__)
_STATE_SAVE_TIMEOUT = 30.0


async def resolve_profile(thread_id: str, *, untrusted: bool) -> str | None:
    """The shared profile a thread's new session may use; None for an untrusted one (the C3 gate)."""
    if untrusted:
        return None
    return str(await asyncio.to_thread(browser_launch.profile_dir, thread_id))


def assign(sessions: Mapping[str, Any], profile: str | None) -> tuple[str | None, str | None]:
    """Give a new session (profile, copy_of): the profile when free, else the holder it copies from."""
    if profile is None:
        return None, None
    holder = next((runtime.record.thread_id for runtime in sessions.values()
                   if runtime.record.profile == profile), None)
    return (None, holder) if holder is not None else (profile, None)


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
    if holder is None or holder.record.binding is None or holder.record.terminal_state != "running":
        return
    target = browser_launch.copy_path(record.session)
    try:
        async with holder.command_lock:
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
