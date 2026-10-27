"""Which saved sign-in a managed browser session launches with, and the copy a concurrent one starts from.

Chrome locks a profile to one running browser. The first session of an owner holds the shared profile; a
session that starts while it is held launches on a throwaway profile seeded with a copy of the holder's
sign-in (agent-browser's private ``state save`` / ``state load``), kept in private state and removed on close.
A session whose owning user cannot be resolved receives neither the profile nor a copy.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from host_access import browser_cli, browser_executables, browser_identity as identity, browser_launch

logger = logging.getLogger(__name__)
_STATE_SAVE_TIMEOUT = 30.0
_SIGNIN_MISSED = "Saved sign-in wasn't used this time. This browser starts signed out."


class CopyRestoreError(RuntimeError):
    """The copied sign-in could not be restored before the first action."""


async def pin_native(owner: Any, *, changed_message: str) -> None:
    """Resolve and pin both native executables before session admission."""
    from host_access.browser_session_state import BrowserSetupError
    async with owner._native_lock:
        try:
            path, fingerprint = await asyncio.to_thread(browser_cli.resolve_native, owner.binary)
            if owner._native is not None:
                if (path, fingerprint) != owner._native_source:
                    raise identity.BrowserIdentityChanged(changed_message)
                return
            source_identity = (path, fingerprint)
            if sys.platform == "darwin":
                path = await asyncio.to_thread(browser_executables.private_native, path)
            path, fingerprint = await asyncio.to_thread(browser_cli.resolve_native, path)
            owner.socket_dir = await asyncio.to_thread(identity.ensure_private_dir, owner.socket_dir)
            from host_access.browser_environment import prepare_config
            await asyncio.to_thread(prepare_config, owner.socket_dir)
            result = await owner._invoke((path, "--version"), timeout=5.0)
            if result.completion != "exited" or result.timed_out or result.returncode:
                raise identity.BrowserIdentityError(
                    f"`agent-browser --version` did not succeed; {browser_cli.install_hint()}")
            version = browser_cli.admitted_version(result.stdout)
            if await asyncio.to_thread(browser_cli.resolve_native, owner.binary) != source_identity:
                raise identity.BrowserIdentityChanged(changed_message)
            chrome, source = await select_chrome(owner, path)
            user_agent = browser_launch.user_agent(await asyncio.to_thread(browser_launch.chrome_major, chrome))
            await asyncio.to_thread(prepare_directories)
            owner._native_source = source_identity
            owner._native = browser_cli.NativeExecutable(path, fingerprint, version)
            owner._chrome = chrome
            owner._user_agent = user_agent
            logger.info("managed Chrome resolved from %s (%s)", chrome, source)
        except identity.BrowserIdentityError as exc:
            # Setup refusal precedes admission; it never enters cleanup state.
            raise BrowserSetupError(str(exc)) from exc
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise BrowserSetupError("Shared browser setup could not complete; try browser setup again") from exc


async def start_chrome(owner: Any, runtime: Any) -> None:
    """Start the producer before agent-browser is permitted to attach."""
    from host_access.browser_chrome import ChromeProcess
    if sys.platform != "darwin":
        return  # Preserve native-owned Linux launch; saved profiles are refused at admission.
    assert owner._chrome is not None
    from host_access.browser_download import is_private_download
    if not await asyncio.to_thread(is_private_download, owner._chrome):
        await asyncio.to_thread(browser_executables.verify_google, owner._chrome)
    runtime.chrome = await ChromeProcess.launch(
        owner._chrome, profile=runtime.record.profile,
        headless=not runtime.record.show_window, ua=owner._user_agent)
    runtime.chrome.release_stream()
    runtime.attempt_started_at = time.time()
    runtime.attempt = browser_cli.CommandResult(-1, "", "", completion="unknown")
    try:
        runtime.attempt = await owner._invoke(owner._session_argv(runtime, ("stream", "status")), timeout=5)
    except browser_cli.CommandCancelled as exc:
        runtime.attempt = exc.result
        raise
    await owner._observe_attempt(runtime)
    await verify_stream(owner, runtime, runtime.attempt)


async def verify_stream(owner: Any, runtime: Any, result: Any = None) -> None:
    """The native daemon may fall back to another port; refuse that session."""
    from host_access.browser_chrome import require_listener
    import json
    assert runtime.chrome is not None and runtime.record.binding is not None
    if result is None:
        result = await owner._invoke(owner._session_argv(runtime, ("stream", "status")), timeout=5)
    try:
        data = json.loads(result.stdout)
        actual = data.get("data", {}).get("port")
    except (ValueError, AttributeError):
        actual = None
    if result.returncode or result.timed_out or result.completion != "exited" or actual != runtime.chrome.stream_port:
        raise RuntimeError("managed browser stream did not use its reserved listener")
    await asyncio.to_thread(require_listener, runtime.record.binding.pid, actual)


async def close_after_failure(runtime: Any) -> Exception | None:
    """Retain an unresolved Chrome handle without replacing the original cleanup cause."""
    from host_access.browser_session_state import BrowserSessionCleanupError
    if runtime.chrome is None:
        return None
    try:
        await asyncio.shield(runtime.chrome.close())
        runtime.chrome = None
        return None
    except Exception:
        logger.exception("managed Chrome cleanup also failed during session recovery")
        return BrowserSessionCleanupError("managed Chrome cleanup could not be verified")


async def resolve_profile(thread_id: str) -> tuple[str | None, str | None]:
    """Resolve the saved profile and any notice about platform availability."""
    profile = await asyncio.to_thread(browser_launch.profile_dir, thread_id)
    if profile is not None and sys.platform == "linux":
        return None, "Saved sign-in isn't available on Linux. This browser starts signed out and forgets sign-ins when it closes."
    return (str(profile), None) if profile is not None else (None, _SIGNIN_MISSED)


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
    """Keep launch settings stable across the CDP connection's commands."""
    if sys.platform != "darwin":
        return ["--args", browser_launch.stealth_args(ua, headless=not record.show_window),
                "--init-script", str(browser_launch.STEALTH_INIT_SCRIPT)]
    return ["--init-script", str(browser_launch.STEALTH_INIT_SCRIPT)]


async def restore_copy(owner: Any, runtime: Any) -> None:
    """CDP attach ignores launch-time --state; restore privately before the first action."""
    copy = browser_launch.copy_path(runtime.record.session)
    if not runtime.record.copy_of:
        return
    if not copy.is_file():
        logger.warning("browser sign-in copy unavailable at restore")
        runtime.record = replace(runtime.record, profile_notice=_SIGNIN_MISSED)
        return
    runtime.attempt = await owner._invoke(
        owner._session_argv(runtime, ("state", "load", str(copy))), timeout=_STATE_SAVE_TIMEOUT)
    await owner._observe_attempt(runtime)
    if runtime.attempt.returncode or runtime.attempt.timed_out or runtime.attempt.completion != "exited":
        logger.warning("browser sign-in copy could not be restored: completion=%s timed_out=%s exit=%s",
                       runtime.attempt.completion, runtime.attempt.timed_out, runtime.attempt.returncode)
        raise CopyRestoreError("saved sign-in could not be restored")


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
        logger.warning("browser sign-in copy unavailable: holder missing")
        runtime.record = replace(runtime.record, profile_notice=_SIGNIN_MISSED)
        return
    target = browser_launch.copy_path(record.session)
    try:
        async with holder.command_lock:
            if holder.record.binding is None or holder.record.terminal_state != "running":
                logger.warning("browser sign-in copy unavailable: holder not running")
                runtime.record = replace(runtime.record, profile_notice=_SIGNIN_MISSED)
                return
            result = await owner._invoke(
                owner._session_argv(holder, ("state", "save", str(target))), timeout=_STATE_SAVE_TIMEOUT)
        if result.returncode == 0 and not result.timed_out and result.completion == "exited" and target.is_file():
            os.chmod(target, 0o600)
        else:
            logger.warning("browser sign-in copy was not saved: exit=%s", result.returncode)
            target.unlink(missing_ok=True)
            runtime.record = replace(runtime.record, profile_notice=_SIGNIN_MISSED)
    except Exception:
        logger.warning("browser sign-in copy failed", exc_info=True)
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        runtime.record = replace(runtime.record, profile_notice=_SIGNIN_MISSED)


def drop_copy(record: Any) -> None:
    """Delete this session's copy of the sign-in once it is gone."""
    if record.copy_of:
        with contextlib.suppress(OSError):
            browser_launch.copy_path(record.session).unlink()


async def select_chrome(owner: Any, agent_browser: str) -> tuple[str | None, str]:
    """The Chrome agent-browser launches; with none installed, fetch agent-browser's own first."""
    chrome, source = await asyncio.to_thread(browser_cli.chrome_executable)
    if sys.platform == "darwin":
        from host_access import browser_download
        if chrome is None:
            chrome = await asyncio.to_thread(browser_cli.downloaded_chrome_executable)
        if chrome is not None:
            try:
                return await asyncio.to_thread(browser_executables.trusted_chrome, chrome), source
            except identity.BrowserIdentityError:
                logger.info("installed Chrome could not be authenticated; using private download")
        return await browser_download.install(agent_browser, owner.socket_dir), "private-download"
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
