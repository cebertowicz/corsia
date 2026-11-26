"""browser_fill's saved-login path: an agent names a saved login by site and username, and the server fills it.

The agent never sees a password, its length or a login id. Which logins it may use, and the fill itself, are the
fill executor's (host_access.browser_login_fill), read on the server from the calling thread. Here are only the
argument checks, which never clamp, and the current page's address, which only chooses the usernames a no-match
lists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from host_access import browser_cli, browser_login_fill
from tools.browser_runtime import _run

EXCLUSIVE = "Error: saved_login fills a saved login on its own; leave out selector and text."
BAD_LOGIN = "Error: saved_login needs a site (its https address) and a username."
BAD_MINUTES = f"Error: minutes is a whole number from 1 to {browser_login_fill.MAX_MINUTES}."
NO_MATCH = "No saved login you may use matches that site and username."
ON_PAGE = " Saved logins you may use on this page's site: {usernames}."
NONE_ON_PAGE = " You may use no saved login on this page's site."


async def fill(thread_id: str, saved_login: object, minutes: object) -> str:
    """The agent's result text: a refusal of its arguments, a no-match, or the fill executor's own text."""
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)
                                or not 1 <= minutes <= browser_login_fill.MAX_MINUTES):
        return BAD_MINUTES
    named = _named(saved_login)
    if named is None:
        return BAD_LOGIN
    login_id = await asyncio.to_thread(browser_login_fill.login_named, thread_id, *named)
    if login_id is None:
        return NO_MATCH + await _on_page(thread_id)
    requested = None if minutes is None else minutes * 60
    return await browser_login_fill.fill_saved_login(thread_id, login_id, requested_seconds=requested)


def _named(saved_login: object) -> tuple[str, str] | None:
    if not isinstance(saved_login, dict):
        return None
    site, username = saved_login.get("site"), saved_login.get("username")
    if not isinstance(site, str) or not site or not isinstance(username, str) or not username:
        return None
    return site, username


async def _on_page(thread_id: str) -> str:
    url = await _page_url(thread_id)
    usernames = await asyncio.to_thread(browser_login_fill.usable_usernames, thread_id, url) if url else []
    return ON_PAGE.format(usernames=", ".join(usernames)) if usernames else NONE_ON_PAGE


async def _page_url(thread_id: str) -> str | None:
    """The thread's current page address, without opening a browser that isn't open."""
    from host_access.processes import PROCESS_OWNER

    if not await PROCESS_OWNER.browser_sessions().has_session(thread_id):
        return None
    result = await _run(["get", "url"], thread_id=thread_id, notice=False)
    if result.startswith("Error"):
        return None
    url: Any = browser_cli.json_object(result).get("url")
    return url if isinstance(url, str) else None
