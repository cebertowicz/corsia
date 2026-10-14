"""The command path shared by agent tools and the visible browser pane."""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Sequence

from host_access import browser_cdp, browser_cli, browser_control, browser_identity as identity
from host_access.browser_session_state import BrowserSessionError, BrowserSetupError

log = logging.getLogger(__name__)


async def run(owner: Any, thread_id: str, args: Sequence[str], *, lease_id: str | None,
              timeout: float, show_window: bool | None, session: str) -> str:
    """Guard and run one agent command through the session owner's one command path."""
    _validate(thread_id, args, timeout, show_window)
    try:
        await asyncio.to_thread(browser_cli.check_local_mode, args)
    except identity.BrowserIdentityError as exc:
        raise BrowserSetupError(str(exc)) from exc
    await owner._pin_native()
    runtime = await owner._admit(thread_id, lease_id=lease_id, show_window=show_window, session=session)
    command = partial(_command, owner, runtime, lease_id)
    if not (session == "new" and args[0] == "open"):
        await browser_control.require_page_access(thread_id, partial(browser_cdp.password_probe, command), tuple(args))
    if browser_control.has_pane(thread_id):
        result = await browser_cdp.shared_action(command, args)
        if result is not None:
            return result
        if args[0] in {"click", "fill", "type"}:
            log.info("shared_browser_dom_fallback=true")
    result, unfinished = await owner._run_admitted(runtime, args, lease_id=lease_id, timeout=timeout)
    if result.timed_out and args[0] == "wait":
        return browser_cli.WAIT_TIMED_OUT
    rendered = owner._model_result(result, runtime.record)
    if args[0] == "open" and runtime.record.show_window and not unfinished and not rendered.startswith("Error:"):
        browser_control.announce_browser_opened(thread_id)
    return rendered


async def _command(owner: Any, runtime: Any, lease_id: str | None, args: Sequence[str], timeout: float) -> browser_cli.CommandResult:
    result, _unfinished = await owner._run_admitted(runtime, args, lease_id=lease_id, timeout=timeout)
    return result


def _validate(thread_id: str, args: Sequence[str], timeout: float, show_window: bool | None) -> None:
    if not isinstance(thread_id, str) or not thread_id:
        raise BrowserSessionError("browser command requires exact thread identity")
    invalid_preference = show_window is not None and not isinstance(show_window, bool)
    if not args or not all(isinstance(arg, str) and arg for arg in args) or timeout <= 0 or invalid_preference:
        raise BrowserSessionError("browser command is invalid")
    if args[0] in {"close", "quit", "exit"}:
        raise BrowserSessionError("browser close requires owned session teardown")
