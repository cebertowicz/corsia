"""The command path shared by agent tools and the visible browser pane."""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Sequence

from host_access import browser_cdp, browser_cli, browser_control, browser_identity as identity
from host_access.browser_session_state import BrowserSessionError, BrowserSetupError

log = logging.getLogger(__name__)

# A pane's own commands: the person's navigation, the page metadata their pane shows, and their pane's size
# and theme. They are not agent commands, so the password guard (AR3, AR4) does not refuse them.
_PANE_COMMANDS = {("open",), ("back",), ("forward",), ("reload",), ("get", "cdp-url"), ("get", "url"),
                  ("get", "title"), ("set", "viewport"), ("set", "media")}


async def run(owner: Any, thread_id: str, args: Sequence[str], *, lease_id: str | None,
              timeout: float, show_window: bool | None, session: str, pane: bool = False) -> str:
    """Guard agent commands; a connected pane's own commands pass, so the person can leave a password page."""
    _validate(thread_id, args, timeout, show_window, pane)
    try:
        await asyncio.to_thread(browser_cli.check_local_mode, args)
    except identity.BrowserIdentityError as exc:
        raise BrowserSetupError(str(exc)) from exc
    await owner._pin_native()
    runtime = await owner._admit(thread_id, lease_id=lease_id, show_window=show_window, session=session)
    command = partial(_command, owner, runtime, lease_id)
    if not pane and not (session == "new" and args[0] == "open"):
        await browser_control.require_page_access(thread_id, partial(browser_cdp.password_probe, command), tuple(args))
    if not pane:
        # Only a command the guard let through puts the agent in the page.
        browser_control.agent_acted(thread_id)
    if browser_control.has_pane(thread_id):
        # A pane command never fills or types (_PANE_COMMANDS), so a marked field is always the agent's.
        result = await browser_cdp.shared_action(command, args, partial(browser_control.agent_acted, thread_id))
        if result is not None:
            return result
        if args[0] in {"click", "fill", "type"}:
            log.info("shared_browser_dom_fallback=true")
    browser_control.page_command_starting(thread_id, tuple(args))
    try:
        result, unfinished = await owner._run_admitted(runtime, args, lease_id=lease_id, timeout=timeout)
    finally:
        # A held pane shows nothing until this restart, so it runs however the command ends.
        await browser_control.page_command_finished(thread_id, tuple(args))
    if result.timed_out and args[0] == "wait":
        return browser_cli.WAIT_TIMED_OUT
    rendered = owner._model_result(result, runtime.record)
    if args[0] == "open" and runtime.record.show_window and not unfinished and not rendered.startswith("Error:"):
        browser_control.announce_browser_opened(thread_id)
    return rendered


async def _command(owner: Any, runtime: Any, lease_id: str | None, args: Sequence[str], timeout: float) -> browser_cli.CommandResult:
    result, _unfinished = await owner._run_admitted(runtime, args, lease_id=lease_id, timeout=timeout)
    return result


def _validate(thread_id: str, args: Sequence[str], timeout: float, show_window: bool | None,
              pane: bool) -> None:
    if not isinstance(thread_id, str) or not thread_id:
        raise BrowserSessionError("browser command requires exact thread identity")
    invalid_preference = show_window is not None and not isinstance(show_window, bool)
    if not args or not all(isinstance(arg, str) and arg for arg in args) or timeout <= 0 or invalid_preference:
        raise BrowserSessionError("browser command is invalid")
    if args[0] in {"close", "quit", "exit"}:
        raise BrowserSessionError("browser close requires owned session teardown")
    if pane and tuple(args[:1]) not in _PANE_COMMANDS and tuple(args[:2]) not in _PANE_COMMANDS:
        raise BrowserSessionError("a browser pane only navigates, reads its page's address and title, and sizes the page")
