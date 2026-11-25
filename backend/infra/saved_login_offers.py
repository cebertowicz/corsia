"""The offer to save a login the person just signed in with, held in server memory only.

When the person signs in to a site themselves in the agents' browser, the
browser's capture of their own submit hands the site, username and password to
:func:`offer_login`, and calls :func:`confirm_offer` once that sign-in settles
cleanly. Only a confirmed offer is shown to the person or can be saved. The
values stay in this process's memory, in one offer per browser session, until
the person chooses Save (written through ``infra.saved_logins.save_login``) or
Not now. The offer is also dropped when
the tab leaves the offer's site (:func:`drop_offer`), when the browser session
closes (``host_access.browser_control.browser_closed``), and when the server
restarts, since nothing here is ever written down.

A browser session's key is ``"<thread id>:<session identity>"``: a relaunched
browser has a new identity, so an offer from an earlier session is never shown.
The person's routes (``api.saved_logins``) resolve the current key with
:func:`session_key_for`; the capture passes the same string.

No agent tool, event, websocket message, plan step or log names an offer, and
no view carries the password. A test pins which modules may name
:func:`offer_login`, :func:`confirm_offer`, :func:`note_username` and
``password_matches``.
Errors raised here carry fixed text and never a value.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field, replace

from infra.key_store_values import KeyStoreError
from infra.own_sites import is_own_site
from infra.saved_logins import (
    logins_for_site, normalized_site, password_matches, save_login, validated_password, validated_username,
)

_SESSION_REFUSAL = (
    "The shared browser this sign-in came from has closed, so it can’t be saved. "
    "It’s offered again the next time you sign in."
)
_OWN_SITE_REFUSAL = "CORSIA doesn’t save a login for CORSIA itself."
NO_OFFER = "Saving isn’t available for this sign-in. Unless the login is already saved, it’s offered again the next time you sign in."


@dataclass(frozen=True)
class OfferView:
    """What the person is shown about a pending offer. It has no password field, by design."""

    site: str
    #: The site's host for display: no scheme, and a port only when it isn't 443.
    domain: str
    username: str
    #: Whether a login for this site and username is already saved, so Save replaces its password.
    update: bool


@dataclass(frozen=True)
class _Offer:
    site: str
    username: str
    password: str = field(repr=False)
    update: bool
    confirmed: bool = False

    def view(self) -> OfferView:
        """What the person may see of this offer."""
        return OfferView(self.site, self.site.removeprefix("https://"), self.username, self.update)


_lock = threading.Lock()
_offers: dict[str, _Offer] = {}
#: The username from the first page of a two-page sign-in: session key to (site, username).
_usernames: dict[str, tuple[str, str]] = {}
#: The offer_login or save_offer call still working for each session. A drop or a newer offer_login
#: removes or replaces its claim, so an offer checked or put back after a drop is never stored.
_claims: dict[str, object] = {}


def session_key_for(thread_id: str) -> str | None:
    """The key of the thread's current browser session, or None when it has no running browser."""
    from host_access import browser_pane

    session = browser_pane.session_identity(thread_id)
    return session_key(thread_id, session) if session else None


def session_key(thread_id: str, session: str) -> str:
    """The one format of an offer's session key; capture, session end and the routes all build it here."""
    return f"{thread_id}:{session}"


def _key(session_key: object) -> str:
    if not isinstance(session_key, str) or ":" not in session_key or session_key.startswith(":"):
        raise KeyStoreError(_SESSION_REFUSAL)
    return session_key


def note_username(session_key: str, site: str, username: str) -> None:
    """Keep the username from a sign-in's first page, for the next offer on this session and site."""
    key, clean_site, clean_username = _key(session_key), normalized_site(site), validated_username(username)
    with _lock:
        _usernames[key] = (clean_site, clean_username)


def offer_login(
    session_key: str, site: str, username: str | None, password: str, *, db: sqlite3.Connection | None = None,
) -> bool:
    """Hold an unconfirmed offer to save this sign-in; return whether one is now held.

    Call at the person's own submit, before it is known to have worked, and
    :func:`confirm_offer` once it has. Every call replaces the session's earlier
    offer, even when it makes none. With no username, the one noted for this
    session and site is used; with none noted, no offer is made. A confirmed
    sign-in uses up the noted username, so a click before the real submit or a
    failed attempt leaves it for the next submit. No offer is made when this login is already saved
    with this password; a different saved password makes an update offer.
    CORSIA's own address is refused. Reads the database, so call it off the event loop.
    """
    key, claim = _key(session_key), object()
    with _lock:
        _offers.pop(key, None)
        noted = _usernames.get(key)
        _claims[key] = claim
    try:
        offer = _checked_offer(site, username, password, noted, db)
        with _lock:
            if offer is not None and _claims.get(key) is claim:
                _offers[key] = offer
                return True
        return False
    finally:
        with _lock:
            if _claims.get(key) is claim:
                del _claims[key]


def _checked_offer(
    site: str, username: str | None, password: str, noted: tuple[str, str] | None, db: sqlite3.Connection | None,
) -> _Offer | None:
    """The offer this sign-in makes, or None when it makes none. Raises on CORSIA's own address."""
    clean_site, clean_password = normalized_site(site), validated_password(password)
    if is_own_site(clean_site, db=db):
        raise KeyStoreError(_OWN_SITE_REFUSAL)
    if not username:
        if noted is None or noted[0] != clean_site:
            return None
        username = noted[1]
    clean_username = validated_username(username)
    saved = next((login for login in logins_for_site(clean_site, db=db) if login.username == clean_username), None)
    if saved is not None and password_matches(saved.id, clean_password, db=db):
        return None
    return _Offer(clean_site, clean_username, clean_password, update=saved is not None)


def confirm_offer(session_key: str) -> bool:
    """Mark the session's offer confirmed once the person's sign-in settled cleanly; return whether one was held.

    Confirming uses up the session's noted username.
    """
    key = _key(session_key)
    with _lock:
        offer = _offers.get(key)
        if offer is not None:
            _offers[key] = replace(offer, confirmed=True)
            _usernames.pop(key, None)
    return offer is not None


def pending_offer(session_key: str | None) -> OfferView | None:
    """The session's confirmed offer as the person may see it, or None. Never the password."""
    if session_key is None:
        return None
    with _lock:
        offer = _offers.get(session_key)
    return offer.view() if offer is not None and offer.confirmed else None


def save_offer(session_key: str | None, *, saved_by: str, db: sqlite3.Connection | None = None) -> str:
    """Save the session's pending offer as the person ``saved_by``; return the login id.

    An unconfirmed offer is refused and kept. A confirmed one and the noted
    username leave memory first, so a second Save can't save it twice. A failed
    save keeps the offer so Save can be pressed again: it is put back unless Not
    now, the browser closing or a newer sign-in came meanwhile. The noted
    username is not put back; confirming already used it.
    """
    claim = object()
    with _lock:
        offer = _offers.get(session_key) if session_key is not None else None
        if session_key is None or offer is None or not offer.confirmed:
            raise KeyStoreError(NO_OFFER)
        del _offers[session_key]
        _usernames.pop(session_key, None)
        _claims[session_key] = claim
    try:
        return save_login(
            site=offer.site, username=offer.username, password=offer.password, saved_by=saved_by, db=db,
        )
    except BaseException:
        with _lock:
            if _claims.get(session_key) is claim and session_key not in _offers:
                _offers[session_key] = offer
        raise
    finally:
        with _lock:
            if _claims.get(session_key) is claim:
                del _claims[session_key]


def drop_offer(session_key: str | None) -> None:
    """Drop the session's offer, confirmed or not, and its noted username: Not now, or the tab left the site."""
    if session_key is None:
        return
    with _lock:
        _offers.pop(session_key, None)
        _usernames.pop(session_key, None)
        _claims.pop(session_key, None)


def drop_thread(thread_id: str) -> None:
    """Drop every offer and noted username of the thread's browser sessions, when its browser closes."""
    prefix = f"{thread_id}:"
    with _lock:
        for held in (_offers, _usernames, _claims):
            for key in [key for key in held if key.startswith(prefix)]:
                held.pop(key, None)
