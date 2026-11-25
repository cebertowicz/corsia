"""Type a saved login into the shared browser's page once the person has approved it (the fill executor).

`fill_saved_login` is server code; no agent verb or tool calls it in this version. In order: the person's
approval (infra.saved_login_fills), a clean document (browser_fill_marks: no agent script ran in it or on its
site this session, no agent change outlives its documents, and no other window can reach it), where the values go (browser_login_page:
C5, C7), the fill itself, and the sign-in's result (infra.saved_login_results). It holds the session's action
lock throughout, so no agent command runs in the page between the checks and the fill.

This is one of the two modules allowed to read a saved password (tests/test_saved_logins_leaks.py). The value
is read with `may_fill` immediately before it, lives only in `_type`'s locals and the CDP message that types
it, and is never logged, raised, kept, sent in an event or returned. Every text returned is fixed, or is
Governance's approved result text; none carries page data.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from host_access import browser_cdp, browser_control, browser_fill_marks, browser_login_page as page
from host_access import browser_session_commands, browser_step_lifecycle
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import COMMAND_TIMEOUT, BrowserSessionError
from infra import saved_login_fills, saved_login_results, saved_logins
from infra.database import get_db

log = logging.getLogger(__name__)
# How long after submitting the page may take to settle before the result counts as unknown: as long as an agent's
# own browser command may wait on a page. The password step has no limit of its own; the person ends it.
SETTLE_LIMIT = COMMAND_TIMEOUT
ASKED = ("The person has been asked to approve filling the saved login for {site} ({username}). Nothing was "
         "filled; try again once they approve it.")
NO_BROWSER = "No browser is open for this thread, so nothing was filled. Open the sign-in page first."
NOT_CLEAN = ("The saved login for {site} wasn't filled: an agent's script or browser change, or another open window, "
             "could reach the sign-in page.")
UNKNOWN = ("The saved login for {site} ({username}) was filled and sent, but the page didn't show whether the sign-in "
           "worked. Take a snapshot to see where the page is.")
# Every other status's line; each is formatted with the login's site and username.
REFUSED = {
    "no_field": ("The saved login for {site} wasn't filled: this page doesn't show exactly one password field. If it "
                 "asks for the username first, enter {username}, go on to the password page and try again."),
    "http_frame": "The saved login wasn't filled: the password field isn't on a secure (https) page.",
    "frame_origin": "The saved login for {site} wasn't filled: the password field belongs to another site.",
    "form_action": "The saved login for {site} wasn't filled: the sign-in form sends to another site.",
    "insecure_action": "The saved login for {site} wasn't filled: the sign-in form doesn't send securely (https).",
    "get_form": "The saved login for {site} wasn't filled: the sign-in form would put the password in the page address.",
    "changed": "The saved login for {site} wasn't filled: the sign-in form changed while it was being checked.",
    "no_grant": "The saved login for {site} wasn't filled: the person's approval ended before it could be used.",
    "gone": "The saved login wasn't filled: it's no longer saved.",
    "page": ("The saved login for {site} wasn't filled: the page didn't respond as expected. Take a snapshot to see "
             "where it is."),
    "uncertain": ("The saved login for {site} ({username}) may be in the page, and its form may have been sent: the "
                  "page didn't respond as expected. Take a snapshot to see where the page is."),
    "unsubmitted": ("The saved login for {site} was filled, but its form couldn't be sent. Take a snapshot and "
                    "use the page's sign-in button."),
}
# Statuses whose line leads the person's own sign-in step.
_PERSON_SIGNS_IN = {"not_clean": NOT_CLEAN, "get_form": REFUSED["get_form"]}
# The ends after which the sign-in's result is known and recorded by its own result function.
_RESULT_KNOWN = frozenset({"signed_in", "rejected"})


def _owner() -> Any:
    from host_access.processes import PROCESS_OWNER
    return PROCESS_OWNER.browser_sessions()


async def fill_saved_login(thread_id: str, login_id: str) -> str:
    """Fill a saved login into the thread's shared page and return the agent's result text."""
    if not await asyncio.to_thread(saved_login_fills.may_fill, thread_id, login_id):
        return await _ask(thread_id, login_id)
    try:
        login = await asyncio.to_thread(saved_logins.get_login, login_id)
    except ValueError:
        return REFUSED["gone"]
    owner = _owner()
    try:
        await owner._pin_native()
        runtime = await owner._admit(thread_id, lease_id=None, show_window=None, session="existing")
    except BrowserSessionError:
        return NO_BROWSER
    async with runtime.action_lock:
        try:
            status = await _fill(thread_id, login, runtime, partial(browser_session_commands._command, owner, runtime, None))
        except Exception as exc:  # noqa: BLE001 - any failure is the fixed page line; nothing typed escapes in it
            log.warning("saved-login fill stopped: %s", type(exc).__name__)
            status = "page"
    return await _result(owner, thread_id, login, status)


async def _ask(thread_id: str, login_id: str) -> str:
    """No live approval: ask the person for one, or give them the sign-in when the login is out of date."""
    try:
        await asyncio.to_thread(saved_login_fills.request_fill, thread_id, login_id)
    except saved_login_fills.FillApprovalError as refusal:
        text = str(refusal)
        try:
            login = await asyncio.to_thread(saved_logins.get_login, login_id)
        except ValueError:
            return text
        if login.status != saved_logins.STATUS_OUT_OF_DATE:
            return text
        return await _person_step(_owner(), thread_id, login, text)
    try:
        login = await asyncio.to_thread(saved_logins.get_login, login_id)
    except ValueError:
        return REFUSED["gone"]
    return ASKED.format(site=login.site, username=login.username)


async def _fill(thread_id: str, login: saved_logins.SavedLogin, runtime: Any, command: Any) -> str:
    """Fill a clean document; any agent mark on it, its site or the session, or a linked window, is 'not_clean'.

    The page is never reloaded: a site or session mark outlives a reload, so it can't make the page clean, and
    it would lose the person's progress on it.
    """
    session, site = runtime.record.session, saved_logins.normalized_site(login.site)
    domain = registrable_domain(urlsplit(site).hostname or "")
    endpoint = await browser_cdp.page_endpoint(command)
    browser = await browser_cdp.browser_endpoint(command, page.PAGE_CHECK_FAILED)
    async with browser_cdp.CdpClient(endpoint) as cdp:
        loader = browser_cdp.loader_id(await cdp.call("Page.getFrameTree"))
        if not await _clean(thread_id, session, loader, domain, browser, endpoint):
            return "not_clean"
        return await _fill_on(cdp, thread_id, login, session, endpoint, loader, domain)


async def _clean(thread_id: str, session: str | None, loader: str, domain: str, browser: str, endpoint: str) -> bool:
    """No agent change in the session, no agent script in this document or on the saved site, no linked window."""
    if (browser_fill_marks.session_marked(thread_id, session) or browser_fill_marks.scripted(thread_id, loader)
            or browser_fill_marks.site_marked(thread_id, session, domain)):
        return False
    async with browser_cdp.CdpClient(browser) as cdp:
        return not await page.linked_windows(cdp, endpoint.rstrip("/").rsplit("/", 1)[-1])


async def _fill_on(cdp: browser_cdp.CdpClient, thread_id: str, login: saved_logins.SavedLogin, session: str | None,
                   endpoint: str, loader: str, domain: str) -> str:
    form = await page.find_form(cdp, saved_logins.normalized_site(login.site), saved_logins.normalized_site)
    if isinstance(form, str):
        return form
    if browser_cdp.loader_id(await cdp.call("Page.getFrameTree")) != loader:
        return "changed"
    requests = page.Requests()
    try:
        await requests.start(endpoint)
        # Before anything is typed: this document under the filled-field allowlist, and the session under the
        # saved-sign-in command limit. An unread loader id counts as every document.
        browser_control.mark_saved_fill(thread_id, session, loader)
        status = await _type(cdp, form, thread_id, login)
        if status == "no_grant":
            return status
        try:
            # From here the value may be in the page, so every end records a result exactly once, and a failure
            # is never reported as nothing filled: 'uncertain' until the submit answers, then 'unknown' until
            # the page settles.
            if status == "typed":
                status = "uncertain"
                requests.submitted(asyncio.get_running_loop().time())
                if await page.submit(cdp, form) in {"enter", "clicked"}:
                    status = "unknown"
                    status = await _settled(endpoint, domain, requests)
                else:
                    status = "unsubmitted"
            return status
        except Exception as exc:  # noqa: BLE001 - reduced to its type; the result is the status reached so far
            log.warning("saved-login fill stopped after typing began: %s", type(exc).__name__)
            return status
        finally:
            if status not in _RESULT_KNOWN:
                await _record_unknown(thread_id, login)
    finally:
        await requests.stop()


async def _record_unknown(thread_id: str, login: saved_logins.SavedLogin) -> None:
    try:
        await asyncio.to_thread(saved_login_results.fill_unknown, thread_id, login.id)
    except Exception as exc:  # noqa: BLE001 - a failed audit write must not replace the fill's own result
        log.warning("saved-login fill result couldn't be recorded: %s", type(exc).__name__)


async def _type(cdp: browser_cdp.CdpClient, form: page.SignInForm, thread_id: str, login: saved_logins.SavedLogin) -> str:
    """'no_grant' when nothing was read, so nothing was typed; else type_values's status, or 'uncertain'."""
    text = await asyncio.to_thread(_saved_value, thread_id, login.id)
    if text is None:
        return "no_grant"
    try:
        return await page.type_values(cdp, form, login.username, text)
    except Exception as exc:  # noqa: BLE001 - the error is reduced to its type; the value never leaves here
        log.warning("saved-login fill couldn't type: %s", type(exc).__name__)
        return "uncertain"
    finally:
        del text


def _saved_value(thread_id: str, login_id: str) -> str | None:
    """The saved value, only under a live approval checked immediately before it is read."""
    db = get_db()
    try:
        if not saved_login_fills.may_fill(thread_id, login_id):
            return None
        return saved_logins.password_for_fill(login_id, db=db)
    except ValueError:
        return None
    finally:
        db.close()


async def _settled(endpoint: str, site_domain: str, requests: page.Requests) -> str:
    """Wait for the page to settle as the password step does: 'signed_in', 'rejected', or 'unknown'.

    It re-probes REPROBE_AFTER_INPUT after the submit and after each change, and settles once the page's
    document, host and visible password field have held for REPROBE_EVERY with no sign-in request started
    since the submit still open. One still open at SETTLE_LIMIT, however slow, leaves the result 'unknown':
    a slow server is never read as a rejected password.
    """
    clock = asyncio.get_running_loop().time
    deadline, last, still_since = clock() + SETTLE_LIMIT, None, clock()
    while clock() < deadline:
        await asyncio.sleep(browser_step_lifecycle.REPROBE_AFTER_INPUT)
        try:
            # A CdpClient logs no frame, so a typed value a page keeps in an attribute never reaches a log.
            probe = await browser_cdp.page_password_probe(endpoint, {})
        except Exception:  # noqa: BLE001 - a page mid-navigation can't be probed; the next probe decides
            last = None
            continue
        now, state = clock(), (probe.visit, probe.host, bool(probe.visible_nodes))
        if state != last or requests.open_since_submit():
            last, still_since = state, now
            continue
        if now - still_since < browser_step_lifecycle.REPROBE_EVERY:
            continue
        if registrable_domain(probe.host) != site_domain:
            return "unknown"
        if not probe.visible_nodes:
            return "signed_in"
        if requests.seen:
            return "rejected"
    return "unknown"


async def _result(owner: Any, thread_id: str, login: saved_logins.SavedLogin, status: str) -> str:
    """The agent's text for a status; exactly one result function after a typed fill that settled."""
    if status == "signed_in":
        return await asyncio.to_thread(saved_login_results.fill_succeeded, thread_id, login.id)
    if status == "rejected":
        text = await asyncio.to_thread(saved_login_results.fill_failed, thread_id, login.id)
        return await _person_step(owner, thread_id, login, text)
    if status in _PERSON_SIGNS_IN:
        return await _person_step(owner, thread_id, login, _PERSON_SIGNS_IN[status].format(site=login.site))
    line = UNKNOWN if status == "unknown" else REFUSED[status]
    return line.format(site=login.site, username=login.username)


async def _person_step(owner: Any, thread_id: str, login: saved_logins.SavedLogin, lead: str) -> str:
    """Give the sign-in to the person; `lead` with the step's text, or DECLARATION_NOT_RECORDED."""
    try:
        await owner.declare_person_step(thread_id, f"Sign in to {login.site} yourself")
    except BrowserSessionError as exc:
        text = str(exc)
        if text in {browser_control.PERSON_STEP_DECLINED, browser_control.PASSWORD_DECLINED}:
            return f"{lead} {text}"
        # A step already open on this page (a password page or a person step) is the person's sign-in.
        if not text.startswith((browser_control.PASSWORD_PAGE, browser_control.PERSON_STEP)):
            return browser_control.DECLARATION_NOT_RECORDED
    except Exception as exc:  # noqa: BLE001 - an unrecorded step is reported to the agent as such
        log.warning("saved-login person step couldn't be declared: %s", type(exc).__name__)
        return browser_control.DECLARATION_NOT_RECORDED
    return f"{lead} {browser_control.PERSON_STEP}{browser_control.seen_by_person(thread_id)}"
