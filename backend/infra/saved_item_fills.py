"""The person's timed yes before an agent's saved item is filled into a page.

A saved item (``infra.saved_items``) is approved exactly as a saved login is:
the same store, the person's same approve, deny and revoke, the same list,
durations, "Until I turn it off" (the person's alone), events and restart
behaviour, all in ``infra.saved_login_fills``. Only what names the item lives
here: :func:`request_item_fill` raises its request, :func:`granted_site` reads
its grant, and :func:`end_for_item` ends its approvals when its value is
cleared or its entry removed. :func:`usable_item` is the check a fill request
and a script that uses the item (``automation.script_items``) both run.

A login is filled only on the site it was saved for. An item has no site of
its own, so its request carries the https origin of the frame holding the
field the agent named, and the person's grant is for that site alone.
Grants are keyed by thread and item: while one is waiting or live, asking
again returns it as it is, whatever site the field is on now.

Nothing here reads or holds an item's value.
"""

from __future__ import annotations

from typing import Any

import infra.saved_items as saved_items  # mypy resolves a new submodule only this way
from infra import saved_login_fills, saved_logins
from infra.key_store_values import KeyStoreError
from infra.saved_fill_identity import FillApprovalError, own_thread
from infra.saved_login_fills import ITEM

_NOT_SECURE = "A saved item is filled only on a site whose address starts with https://."
_UNSET = 'The saved item "{label}" has no value yet. The person sets it in Settings › Keys.'


def _same(name: str):
    return lambda approval: approval.kind == ITEM and approval.item == name


def usable_item(thread_id: str, name: str) -> saved_items.SavedItem:
    """The saved item the thread may use, refused when it isn't the person's own saved item or has no value."""
    own_thread(thread_id)
    try:
        item = saved_items.get_item(name)
    except KeyStoreError as exc:
        raise FillApprovalError(str(exc)) from None
    if not item.is_set:
        raise FillApprovalError(_UNSET.format(label=item.label))
    return item


def request_item_fill(thread_id: str, name: str, site: str, *, requested_seconds: object = None) -> dict[str, Any]:
    """Ask the person to approve this thread filling this saved item on ``site``; return the request.

    ``site`` is the https origin of the frame holding the field, read by the fill executor. The duration
    rules are :func:`infra.saved_login_fills.request_fill`'s: an agent never asks for a standing grant.
    """
    requested = None if requested_seconds is None else saved_login_fills._duration(requested_seconds)
    clean_thread = own_thread(thread_id)
    item = usable_item(clean_thread, name)
    try:
        clean_site = saved_logins.normalized_site(site)
    except KeyStoreError:
        raise FillApprovalError(_NOT_SECURE) from None
    return saved_login_fills.open_request(
        clean_thread, saved_login_fills.thread_record(clean_thread), requested, _same(item.name),
        kind=ITEM, login_id=None, item=item.name, item_label=item.label, site=clean_site, username="",
    )


def granted_site(thread_id: str, name: str) -> str | None:
    """The site of the live grant for exactly this thread and item, while the item is still fillable; else None.

    The running turn must be that thread. An item cleared or removed since ends its approvals here too, so a
    grant never outlives its value even if a path that clears it didn't call :func:`end_for_item`.
    """
    if not isinstance(name, str) or not name:
        return None
    grant = saved_login_fills.live_grant(thread_id, _same(name))
    if grant is None:
        return None
    try:
        usable = saved_items.get_item(name).is_set
    except KeyStoreError:
        usable = False
    if usable:
        return grant.site
    end_for_item(name)
    return None


def end_for_item(name: str) -> None:
    """End every request and grant for the item, standing ones included (``fill_revoked``). The key store calls
    this when an entry's value is cleared or the entry removed, so setting it again can't revive a grant."""
    saved_login_fills.end_where(_same(name), deleted=True)
