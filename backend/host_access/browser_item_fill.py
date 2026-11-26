"""Fill a saved item into the field an agent named, once the person has approved it for that field's site.

A saved item (infra.saved_items) is filled through the saved-login fill's own pieces, not a copy of them: the same
browser admission and action lock, the same clean-page check (browser_login_fill.clean_page), the same frame and
form checks (browser_login_page), the same saved-fill session limits (browser_saved_fills) and the same approval
store (through infra.saved_item_fills). What differs is what the item lacks: it has no
site of its own, so the request carries the https origin of the frame holding the field and the fill happens only
on that origin; and it isn't a sign-in, so nothing is submitted and no person sign-in step follows.

This is one of the modules allowed to read a saved item's value (tests/test_saved_logins_leaks.py). The value is
read only after the live grant's site is checked immediately before it, lives only in `_put`'s locals and the
CDP message that fills it, and is never logged, raised, kept, sent in an event or returned. Every text returned
is fixed apart from the item's label and the approved site.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from host_access import browser_cdp, browser_dom_write, browser_login_page as page, browser_saved_fills
from host_access import browser_login_fill, browser_session_commands
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import COMMAND_TIMEOUT, BrowserSessionError
import infra.saved_items as saved_items  # mypy resolves a new submodule only this way
from infra import saved_item_fills, saved_login_results, saved_logins
from infra.database import get_db
from infra.key_store_values import KeyStoreError
from infra.saved_fill_identity import FillApprovalError

log = logging.getLogger(__name__)
ASKED = ('The person has been asked to approve filling the saved item "{label}" on {site}. Nothing was filled; '
         "try again once they approve it.")
FILLED = 'Filled the saved item "{label}" into the field on {site}. Its value isn\'t shown to you.'
GONE = "The saved item wasn't filled: it's no longer saved."
_NOT = 'The saved item "{label}" wasn\'t filled: '
# Every other status's line; each is formatted with the item's label and the approved site.
LINES = {
    "no_field": _NOT + "no field on the page matches that selector. Take a snapshot and use its ref.",
    "not_field": _NOT + "that element isn't a password or text field.",
    "http_frame": _NOT + "the field isn't on a secure (https) page.",
    "frame_origin": _NOT + "the field is on another site than the one the person approved ({site}).",
    "form_action": _NOT + "the field's form sends to another site.",
    "insecure_action": _NOT + "the field's form doesn't send securely (https).",
    "get_form": _NOT + "the field's form would put the value in the page address.",
    "not_clean": _NOT + "an agent's script or browser change, or another open window, could reach this page.",
    "changed": _NOT + "the page changed while the field was being checked.",
    "no_grant": _NOT + "the person's approval ended before it could be used.",
    "page": _NOT + "the page didn't respond as expected. Take a snapshot to see where it is.",
    "uncertain": ('The saved item "{label}" may have been filled into the page on {site}: the page didn\'t respond '
                  "as expected. Take a snapshot to see where the page is."),
    "typed": FILLED,
}


async def fill_saved_item(thread_id: str, name: str, selector: str, *, requested_seconds: int | None = None) -> str:
    """Fill a saved item into the field `selector` names on the thread's shared page; return the agent's text.

    ``requested_seconds`` is only the duration a new request asks for; a live grant is used as it is.
    """
    try:
        item = await asyncio.to_thread(saved_items.get_item, name)
    except KeyStoreError:
        return GONE
    owner = browser_login_fill._owner()
    try:
        await owner._pin_native()
        runtime = await owner._admit(thread_id, lease_id=None, show_window=None, session="existing")
    except BrowserSessionError:
        return browser_login_fill.NO_BROWSER
    async with runtime.action_lock:
        try:
            command = partial(browser_session_commands._command, owner, runtime, None)
            return await _fill(thread_id, item, selector, runtime, command, requested_seconds)
        except Exception as exc:  # noqa: BLE001 - any failure is the fixed page line; nothing filled escapes in it
            log.warning("saved-item fill stopped: %s", type(exc).__name__)
            return LINES["page"].format(label=item.label, site="")


async def _fill(thread_id: str, item: saved_items.SavedItem, selector: str, runtime: Any, command: Any,
                requested_seconds: int | None) -> str:
    """Locate the field; with no live grant ask for one on its frame's site, else check and fill a clean page.

    Everything up to the fill shares one COMMAND_TIMEOUT, as the login fill's steps do, since it holds the
    browser's command lock. The page is never reloaded.
    """
    session = runtime.record.session
    deadline = asyncio.get_running_loop().time() + COMMAND_TIMEOUT
    async with asyncio.timeout_at(deadline):
        endpoint = await browser_cdp.page_endpoint(command)
        browser = await browser_cdp.browser_endpoint(command, page.PAGE_CHECK_FAILED)
    async with browser_cdp.CdpClient(endpoint) as cdp:
        async with asyncio.timeout_at(deadline):
            loader = browser_cdp.loader_id(await cdp.call("Page.getFrameTree"))
            found = await browser_dom_write.locate_target(command, cdp, selector)
            if found is None:
                return _line("no_field", item, "")
            site = await asyncio.to_thread(saved_item_fills.granted_site, thread_id, item.name)
            if site is None:
                origin = (await page.field_frame(found.cdp, found.frame_id))[1]
                if origin is None:
                    return _line("http_frame", item, "")
                return await asyncio.to_thread(_ask, thread_id, item, origin, requested_seconds)
            domain = registrable_domain(urlsplit(site).hostname or "")
            if not await browser_login_fill.clean_page(thread_id, session, loader, domain, browser, endpoint):
                return _line("not_clean", item, site)
            field = await page.item_field(found.cdp, found.frame_id, found.node, site, saved_logins.normalized_site)
            if isinstance(field, str):
                return _line(field, item, site)
            if browser_cdp.loader_id(await cdp.call("Page.getFrameTree")) != loader:
                return _line("changed", item, site)
        # Before anything is filled: this document under the filled-field allowlist (no screenshot of it), and the
        # session under the saved-fill command limit, exactly as for a login.
        browser_saved_fills.mark_saved_fill(thread_id, session, loader)
        status = await _put(found.cdp, field, thread_id, item.name, site, deadline)
    if status in {"typed", "uncertain"}:
        await _record(thread_id, item.name, site, "filled" if status == "typed" else "unknown")
    return _line(status, item, site)


def _line(status: str, item: saved_items.SavedItem, site: str) -> str:
    return LINES[status].format(label=item.label, site=site)


def _ask(thread_id: str, item: saved_items.SavedItem, site: str, requested_seconds: int | None) -> str:
    """No live grant: ask the person for one on this site, or say why it can't be asked for (fixed texts)."""
    try:
        saved_item_fills.request_item_fill(thread_id, item.name, site, requested_seconds=requested_seconds)
    except FillApprovalError as refusal:
        return str(refusal)
    return ASKED.format(label=item.label, site=site)


async def _put(cdp: Any, field: page.ItemField, thread_id: str, name: str, site: str, deadline: float) -> str:
    """'no_grant' when nothing was read, so nothing was filled; else fill_item's status, or 'uncertain'."""
    text = await asyncio.to_thread(_saved_value, thread_id, name, site)
    if text is None:
        return "no_grant"
    try:
        async with asyncio.timeout_at(deadline):
            return await page.fill_item(cdp, field, text)
    except Exception as exc:  # noqa: BLE001 - the error is reduced to its type; the value never leaves here
        log.warning("saved-item fill couldn't fill: %s", type(exc).__name__)
        return "uncertain"
    finally:
        del text


def _saved_value(thread_id: str, name: str, site: str) -> str | None:
    """The item's value, only while this thread's live grant is for exactly this site, checked immediately before."""
    db = get_db()
    try:
        if saved_item_fills.granted_site(thread_id, name) != site:
            return None
        return saved_items.item_value_for_fill(name, db=db)
    except ValueError:
        return None
    finally:
        db.close()


async def _record(thread_id: str, name: str, site: str, result: str) -> None:
    try:
        await asyncio.to_thread(saved_login_results.item_fill_recorded, thread_id, name, site, result)
    except Exception as exc:  # noqa: BLE001 - a failed audit write must not replace the fill's own result
        log.warning("saved-item fill result couldn't be recorded: %s", type(exc).__name__)
