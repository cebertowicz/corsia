"""The saved items an authenticated script declares, and the checks that let a run use them.

A script names each saved item it uses (``automation_schedule``'s ``saved_items``):

- ``{"name": <Settings › Keys entry>, "place": <host>}``: CORSIA sends the value for the script, only to that host
  (``automation.script_item_proxy``);
- ``{"name": <Settings › Keys entry>, "given": true}``: the script holds the value (``automation.script_item_given``);
- ``{"login": <saved login id>}``: a saved login's password, sent only to the host of the site it was saved for.

The declared list is in the application the reviewer reads and the person approves, so it is inside
``application_hash``. The checks are the fills' own (``saved_item_fills.usable_item``,
``saved_fill_identity.usable_login``), run for the script's owner at registration, at every run's start and on
every request. Nothing here reads a value.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from infra import saved_fill_identity, saved_item_fills, saved_logins
from infra.key_store_values import KeyStoreError
from infra.saved_fill_identity import FillApprovalError

_SHAPE = ('saved_items is a list of {"name": <Keys item>, "place": <host>}, {"name": <Keys item>, "given": true} '
          'or {"login": <saved login id>} entries.')
_ONE_PLACE = ('Give the Keys item "{name}" either a place (the one host CORSIA sends it to) or "given": true, '
              "not both.")
_PLACE = ("place is a host name such as api.github.com: lowercase, with no scheme, port, path, IP address or "
          "trailing dot, and not localhost.")
_TWICE = 'saved_items names "{name}" twice; each saved item has one place.'
_LOGIN_GIVEN = "A saved login is never given to a script; CORSIA sends it only to the login's own site."
_LOGIN_PLACE = "A saved login goes only to the site it was saved for, {host}."
_LOGIN_PORT = "A saved login whose site uses a port other than 443 can't be used by a script."
_NEEDS_AUTH = "saved_items needs authenticated=true: the person approves a script that uses a saved item."
_LOGIN_LABEL = "Saved login for {host} ({username})"


class ItemsUnavailable(Exception):
    """A saved item the approval names was removed, cleared, or is no longer one the owner may use."""


@dataclass(frozen=True)
class ItemUse:
    """One approved use: a Keys item (``name``) or a saved login (``login``), and its place, or None when given."""

    name: str | None
    login: str | None
    label: str
    place: str | None

    @property
    def key(self) -> str:
        return self.name if self.name is not None else f"login:{self.login}"


@dataclass(frozen=True)
class RunItems:
    """The saved items one run may use; ``withheld`` when the rule that started it isn't one the person approved."""

    owner: str
    script_id: str
    uses: tuple[ItemUse, ...]
    withheld: bool = False

    def sendable(self) -> tuple[ItemUse, ...]:
        return tuple(use for use in self.uses if use.place is not None)

    def given(self) -> tuple[ItemUse, ...]:
        return tuple(use for use in self.uses if use.place is None)


def _is_address(place: str) -> bool:
    """Whether a resolver would read the name as an IP address, shorthand forms such as ``0x7f.1`` included."""
    try:
        ipaddress.ip_address(place)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(place)
        return True
    except OSError:
        return False


def valid_place(place: object) -> str:
    """The host, refused unless it is a lowercase DNS name (the saved-login check) with no scheme, port, path,
    IP address or trailing dot, and not ``localhost``."""
    if (not isinstance(place, str) or not saved_logins.dns_name(place) or place == "localhost"
            or all(label.isdigit() for label in place.split(".")) or _is_address(place)):
        raise ValueError(_PLACE)
    return place


def _login_entry(thread_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    if entry.get("given") is not None:
        raise ValueError(_LOGIN_GIVEN)
    login, _record = saved_fill_identity.usable_login(thread_id, entry["login"])
    parts = urlsplit(login.site)
    if parts.port not in (None, 443):
        raise ValueError(_LOGIN_PORT)
    host = parts.hostname or ""
    if entry.get("place") is not None and entry["place"] != host:
        raise ValueError(_LOGIN_PLACE.format(host=host))
    return {"login": login.id, "label": _LOGIN_LABEL.format(host=host, username=login.username), "place": host}


def _item_entry(thread_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    given, place = entry.get("given"), entry.get("place")
    if (given is True) == (place is not None) or given not in (None, True):
        raise ValueError(_ONE_PLACE.format(name=entry["name"]))
    item = saved_item_fills.usable_item(thread_id, entry["name"])
    if given:
        return {"name": item.name, "label": item.label, "given": True}
    return {"name": item.name, "label": item.label, "place": valid_place(place)}


def declared(thread_id: str, raw: object, *, authenticated: bool) -> list[dict[str, Any]]:
    """The application's ``saved_items``, checked for the owner thread and sorted; [] when none are named.

    Each refusal is a :class:`ValueError` whose text the agent sees: the fills' own for an item or login the
    owner may not use, and the ones above for the list's shape.
    """
    if raw is None or raw == []:
        return []
    if not authenticated:
        raise ValueError(_NEEDS_AUTH)
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        raise ValueError(_SHAPE)
    entries: list[dict[str, Any]] = []
    for entry in raw:
        if set(entry) - {"name", "login", "place", "given"} or ("name" in entry) == ("login" in entry):
            raise ValueError(_SHAPE)
        try:
            entries.append(_login_entry(thread_id, entry) if "login" in entry else _item_entry(thread_id, entry))
        except FillApprovalError as exc:
            raise ValueError(str(exc)) from None
    keys = [entry.get("name") or f"login:{entry['login']}" for entry in entries]
    for key in keys:
        if keys.count(key) > 1:
            raise ValueError(_TWICE.format(name=key.removeprefix("login:")))
    return sorted(entries, key=lambda entry: ("login" in entry, entry.get("name") or entry.get("login")))


def check_use(owner: str, use: ItemUse) -> None:
    """The fills' scope check for the owner, now: raises :class:`ItemsUnavailable` when it no longer passes."""
    try:
        if use.name is not None:
            saved_item_fills.usable_item(owner, use.name)
        else:
            login, _record = saved_fill_identity.usable_login(owner, use.login)
            if urlsplit(login.site).hostname != use.place:
                raise ItemsUnavailable
    except (FillApprovalError, KeyStoreError):
        raise ItemsUnavailable from None


def _use(entry: dict[str, Any]) -> ItemUse:
    return ItemUse(entry.get("name"), entry.get("login"), str(entry.get("label") or ""), entry.get("place"))


def _rule_approved(authentication: dict[str, Any], rule_id: str) -> bool:
    """Whether the person's approval names this rule and its row is unchanged since."""
    from automation.script_rule_binding import rule_fingerprint
    from infra.database import get_db

    stored = {rule.get("id"): rule.get("fingerprint") for rule in authentication.get("rules") or []}
    if rule_id not in stored:
        return False
    db = get_db()
    try:
        return rule_fingerprint(rule_id, db) == stored[rule_id]
    finally:
        db.close()


def for_run(owner: str, script_id: str, authentication: dict[str, Any], rule_id: str | None) -> RunItems | None:
    """The saved items an approved run may use, None when it names none, or :class:`ItemsUnavailable`.

    A run a rule started gets them only when the approval names that rule and its row is unchanged; otherwise the
    run goes ahead ``withheld``, with no item sockets.
    """
    entries = authentication.get("saved_items") or []
    if not entries:
        return None
    uses = tuple(_use(entry) for entry in entries)
    for use in uses:
        check_use(owner, use)
    withheld = rule_id is not None and not _rule_approved(authentication, rule_id)
    return RunItems(owner, script_id, uses, withheld)


def shown(authentication: dict[str, Any]) -> list[dict[str, Any]]:
    """What the approval panel lists: each item's label and its place, or None when it is given to the script."""
    return [{"label": entry.get("label"), "place": entry.get("place")}
            for entry in authentication.get("saved_items") or []]
