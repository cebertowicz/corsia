"""The command path shared by agent tools and the visible browser pane."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import partial
from typing import Any, Sequence
from urllib.parse import urlsplit

from host_access import browser_cdp, browser_cli, browser_control, browser_dom_write, browser_identity as identity
from host_access import browser_pane_state, browser_step_lifecycle
from host_access.browser_session_state import BrowserSessionCleanupError, BrowserSessionError, BrowserSetupError
from infra.product_events import emit_product_event

# A pane's own commands: the person's navigation, the page metadata their pane shows, and their pane's size
# and theme. They are not agent commands, so the password guard (AR3, AR4) does not refuse them.
_PANE_COMMANDS = {("open",), ("back",), ("forward",), ("reload",), ("get", "url"),
                  ("get", "title"), ("set", "viewport"), ("set", "media")}
# The secret boundary also applies to sessions without a resolved owner.
_COOKIE_COMMANDS = {"cookies", "state", "storage", "auth", "profiles", "stream", "dashboard", "connect",
                    ("network", "har"), ("network", "requests"), ("get", "cdp-url")}
_SECRET_REFUSAL = "browser cookies, saved sign-ins and stored logins are not available to agents"
_SCRIPT_REFUSAL = ("This command is unavailable in a browser session with a saved sign-in. "
                   "Use the browser tools to navigate and interact with the page.")


def _javascript_url(value: str) -> bool:
    try:
        return urlsplit(value).scheme.lower() == "javascript"
    except ValueError:
        return False


def _script_config(config: dict) -> bool:
    """Refuse launch hooks and extensions from native config or the backend environment."""
    names = {"initscripts", "initscript", "extensions", "extension", "plugins", "enable", "restorecheckfn", "args"}
    configured = any(str(key).replace("_", "").replace("-", "").lower() in names and value
                     for key, value in config.items())
    env = ("AGENT_BROWSER_INIT_SCRIPTS", "AGENT_BROWSER_EXTENSIONS", "AGENT_BROWSER_PLUGINS",
           "AGENT_BROWSER_ENABLE", "AGENT_BROWSER_RESTORE_CHECK_FN")
    launch_args = os.environ.get("AGENT_BROWSER_ARGS", "").lower()
    return configured or any(os.environ.get(key) for key in env) or "--load-extension" in launch_args


def _saved_command(args: Sequence[str], *, pane: bool) -> bool:
    """Admit only command shapes produced by the public browser callers."""
    command = args[0]
    if any(_javascript_url(arg) for arg in args[1:]):
        return False
    if any(arg.startswith("-") for arg in args[1:]) and tuple(args) not in {
        ("snapshot", "-i"), ("wait", "--load", "networkidle")
    }:
        return False
    if pane:
        return (command == "open" and len(args) == 2 or
                command in {"back", "forward", "reload"} and len(args) == 1 or
                tuple(args) in {("get", "url"), ("get", "title")} or
                tuple(args[:2]) == ("set", "viewport") and len(args) == 5 or
                tuple(args[:2]) == ("set", "media") and len(args) == 3 and args[2] in {"dark", "light"})
    return (command == "open" and len(args) == 2 or
            tuple(args) in {("snapshot",), ("snapshot", "-i"), ("get", "title"), ("get", "url"),
                            ("wait", "--load", "networkidle")} or
            command == "click" and len(args) == 2 and not args[1].startswith("-") or
            command == "fill" and len(args) == 3 and not args[1].startswith("-") or
            command == "wait" and len(args) == 2 and not args[1].startswith("-") or
            command == "screenshot" and len(args) == 2 and not args[1].startswith("-"))


async def run(owner: Any, thread_id: str, args: Sequence[str], *, lease_id: str | None,
              timeout: float, show_window: bool | None, session: str, pane: bool = False,
              navigation_url: str | None = None, notice: bool = True) -> str:
    """Guard agent commands; a connected pane's own commands pass, so the person can leave a password page."""
    _validate(thread_id, args, timeout, show_window, pane)
    from host_access import browser_navigation
    args = browser_navigation.normalize_command(args)
    if navigation_url is not None:
        normalized_url = browser_navigation.normalize_url(navigation_url)
        if tuple(args) == ("eval", f"location.href = {json.dumps(navigation_url)}; 'navigating'"):
            args = ("eval", f"location.href = {json.dumps(normalized_url)}; 'navigating'")
        navigation_url = normalized_url
    try:
        config = await asyncio.to_thread(browser_cli.check_local_mode, args)
    except identity.BrowserIdentityError as exc:
        raise BrowserSetupError(str(exc)) from exc
    await owner._pin_native()
    runtime = await owner._admit(thread_id, lease_id=lease_id, show_window=show_window, session=session)
    if runtime.record.profile or runtime.record.copy_of:
        trusted_navigation = (navigation_url is not None and
                              tuple(args) == ("eval", f"location.href = {json.dumps(navigation_url)}; 'navigating'") and
                              not _javascript_url(navigation_url))
        if _script_config(config) or not (trusted_navigation and not pane or _saved_command(args, pane=pane)):
            raise BrowserSessionError(_SCRIPT_REFUSAL)
    # Pane and agent commands share this lock, but direct person interaction and
    # lifecycle cleanup do not. The lifecycle lock remains inside _run_admitted.
    owner._queued_actions += 1
    try:
        await runtime.action_lock.acquire()
    finally:
        owner._queued_actions -= 1
    try:
        rendered = await _run_action(owner, runtime, thread_id, args, lease_id, timeout, pane)
        if notice and not pane and runtime.record.profile_notice and not runtime.profile_notice_sent:
            runtime.profile_notice_sent = True
            rendered += "\n" + runtime.record.profile_notice
        return rendered
    finally:
        runtime.action_lock.release()


async def _run_action(owner: Any, runtime: Any, thread_id: str, args: Sequence[str],
                      lease_id: str | None, timeout: float, pane: bool) -> str:
    command = partial(_command, owner, runtime, lease_id)
    opening_unlaunched_session = args[0] == "open" and runtime.record.binding is None
    guarded = not pane and not opening_unlaunched_session
    # A pane command is never a DOM write (_PANE_COMMANDS), so a marked field is always the agent's.
    shared_write = browser_pane_state.has_pane(thread_id) and browser_dom_write.is_dom_write(args)
    # One page target per command: the probe, a read's target and a write all act on the same page.
    endpoint = await browser_cdp.page_endpoint(command) if guarded or shared_write else ""
    if guarded:
        await browser_control.require_page_access(
            thread_id, partial(browser_cdp.page_password_probe, endpoint), tuple(args),
            resolve=partial(browser_dom_write.target_node, command, endpoint), session=runtime.record.session)
    if not pane:
        # Only a command the guard let through puts the agent in the page.
        browser_pane_state.agent_acted(thread_id)
    if shared_write:
        try:
            return await browser_dom_write.shared_action(command, endpoint, args, partial(browser_pane_state.agent_acted, thread_id))
        except browser_dom_write.DomWriteRefused as exc:
            await asyncio.to_thread(emit_product_event, "browser_pane", "agent_write_refused", thread_id=thread_id,
                                    properties={"command": args[0], "reason": exc.reason})
            raise
    browser_pane_state.page_command_starting(thread_id, tuple(args))
    try:
        result, unfinished = await owner._run_admitted(runtime, args, lease_id=lease_id, timeout=timeout)
    finally:
        # A held pane shows nothing until this restart, so it runs however the command ends.
        await browser_pane_state.page_command_finished(thread_id, tuple(args))
    if result.timed_out and args[0] == "wait":
        return browser_cli.WAIT_TIMED_OUT
    rendered = owner._model_result(result, runtime.record)
    # Only the open that starts a session reveals the pane tab, so a tab the person closed stays shut until
    # the next session instead of coming back on every later open.
    if opening_unlaunched_session and not unfinished and not rendered.startswith("Error:"):
        browser_control.announce_browser_opened(thread_id)
    return rendered


async def page_endpoint(owner: Any, thread_id: str) -> str:
    """Resolve a pane's page target through the same resolver and session as agent commands."""
    await owner._pin_native()
    runtime = await owner._admit(thread_id, lease_id=None, show_window=None, session="existing")
    return await browser_cdp.page_endpoint(partial(_command, owner, runtime, None))


async def declare_person_step(owner: Any, thread_id: str, instruction: str) -> None:
    """Give the current shared-browser page to the person and arrange its observed completion."""
    await owner._pin_native()
    runtime = await owner._admit(thread_id, lease_id=None, show_window=None, session="existing")
    command = partial(_command, owner, runtime, None)
    endpoint = await browser_cdp.page_endpoint(command)
    await browser_control.declare_person_step(
        thread_id, partial(browser_cdp.page_password_probe, endpoint), instruction, session=runtime.record.session,
    )


async def close_thread(owner: Any, thread_id: str) -> str:
    """Guard the agent's close command before entering the common session-end path."""
    if not isinstance(thread_id, str) or not thread_id:
        raise BrowserSessionError("browser close requires exact thread identity")
    runtime = owner._sessions.get(thread_id)
    if runtime is None:
        return "Browser session is already closed."
    if browser_step_lifecycle.has_step(thread_id):
        await guard_close(owner, runtime)
    await owner._close_runtime(runtime)
    return "Browser session closed."


async def guard_close(owner: Any, runtime: Any) -> None:
    """Refuse a healthy page still owned by a step; unreachable pages cannot block cleanup."""
    if runtime.record.terminal_state != "running":
        return
    command = partial(_command, owner, runtime, None)
    try:
        endpoint = await browser_cdp.page_endpoint(command)
        await browser_control.require_page_access(
            runtime.record.thread_id, partial(browser_cdp.page_password_probe, endpoint),
            ("close",), session=runtime.record.session,
        )
    except BrowserSessionCleanupError:
        return
    except BrowserSessionError as exc:
        if str(exc) != browser_control.PAGE_CHECK_FAILED:
            raise


async def end_session(thread_id: str, session: str) -> None:
    """Keep a failed step hook observable without preventing producer teardown."""
    try:
        await browser_step_lifecycle.step_event(thread_id, browser_step_lifecycle.Event("session_ended", session=session))
    except Exception:
        logging.getLogger(__name__).exception("browser session-end step hook failed for %s", thread_id)
        browser_step_lifecycle.discard_session(thread_id)
    finally:
        browser_control.browser_closed(thread_id)


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
    if (args[0] in _COOKIE_COMMANDS or tuple(args[:2]) in _COOKIE_COMMANDS or
            any(arg.split("=", 1)[0] in {"--state", "--profile", "--restore", "--restore-save"} for arg in args)):
        raise BrowserSessionError(_SECRET_REFUSAL)
    if pane and tuple(args[:1]) not in _PANE_COMMANDS and tuple(args[:2]) not in _PANE_COMMANDS:
        raise BrowserSessionError("a browser pane only navigates, reads its page's address and title, and sizes the page")
