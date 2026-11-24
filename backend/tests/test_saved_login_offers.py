"""The save offer: held in memory for one browser session, shown and answered only by the person.

An offer is made unconfirmed at the person's submit, confirmed once the sign-in
settles, and only then shown or saved. A new offer replaces the old; Not now,
leaving the site, closing the browser and a restart drop it. CORSIA's own
address is refused. Every route refuses an agent's token, and an import
allowlist pins which modules may make or confirm an offer.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from auth_fixtures import OTHER_USER, OWNER_SESSION_TOKEN, OWNER_TOKEN, OWNER_USER
from fastapi import FastAPI

import infra.saved_login_offers as offers
import infra.saved_logins as saved_logins
from api import saved_logins as saved_logins_api
from host_access import browser_control, browser_pane
from infra.database import get_db
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.devices_names import local_hostname
from infra.key_store_values import KeyStoreError
from infra.settings import set_devices_outside_address

BACKEND = Path(__file__).resolve().parents[1]
SITE = "https://example.com"
#: The only modules that may make, confirm or check an offer. The browser's capture of the
#: person's own submit (host_access/browser_login_capture.py) is added here by its own reviewed change.
SEAM_ALLOWLIST = frozenset({
    "infra/saved_login_offers.py",
    "infra/saved_logins.py",
    "api/saved_logins.py",
    "tests/test_saved_login_offers.py",
    "tests/test_saved_logins_leaks.py",
})
SEAM = ("offer_login", "confirm_offer", "note_username", "password_matches")


@pytest.fixture
def sessions(isolated_db, tmp_path, monkeypatch) -> dict[str, str]:
    """Browser session identities by thread; a thread with none has no open browser."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(offers, "_offers", {})
    monkeypatch.setattr(offers, "_usernames", {})
    monkeypatch.setattr(offers, "_claims", {})
    identities = {"t-mine": "s1"}
    monkeypatch.setattr(browser_pane, "session_identity", identities.get)
    db = get_db()
    for thread_id, user in (("t-mine", OWNER_USER), ("t-other", OTHER_USER)):
        db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) VALUES (?, ?, 'm', 't', 't')",
            (thread_id, user),
        )
    db.commit()
    return identities


@pytest.fixture
def client(sessions):
    app = FastAPI()
    app.include_router(saved_logins_api.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _key(sessions: dict[str, str], thread_id: str = "t-mine") -> str:
    return f"{thread_id}:{sessions[thread_id]}"


def _person(token: str = OWNER_SESSION_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _confirmed(key: str, site: str = SITE, username: str = "me@example.com", password: str = "pw one 1") -> None:
    assert offers.offer_login(key, site, username, password) is True
    assert offers.confirm_offer(key) is True


def test_an_unconfirmed_offer_is_not_shown_or_saved(sessions):
    key = _key(sessions)
    assert offers.offer_login(key, SITE, "me", "pw one 1") is True
    assert offers.pending_offer(key) is None
    with pytest.raises(KeyStoreError, match="There's no sign-in to save here"):
        offers.save_offer(key, saved_by=OWNER_USER)
    assert saved_logins.list_logins() == []
    offers.confirm_offer(key)
    assert offers.pending_offer(key) == offers.OfferView(SITE, "example.com", "me", False)


def test_a_new_offer_replaces_the_old_one(sessions):
    key = _key(sessions)
    _confirmed(key, username="first")
    assert offers.offer_login(key, "https://other.example:8443", "second", "pw two 2") is True
    assert offers.pending_offer(key) is None  # the replacement is not confirmed yet
    offers.confirm_offer(key)
    assert offers.pending_offer(key) == offers.OfferView(
        "https://other.example:8443", "other.example:8443", "second", False,
    )


def test_a_call_that_makes_no_offer_still_replaces_the_old_one(sessions):
    key = _key(sessions)
    _confirmed(key)
    assert offers.offer_login(key, SITE, None, "pw two 2") is False
    assert offers.pending_offer(key) is None


def test_a_two_page_sign_in_uses_the_noted_username_once(sessions):
    key = _key(sessions)
    offers.note_username(key, SITE, "me@example.com")
    assert offers.offer_login(key, "https://elsewhere.example", "", "pw one 1") is False
    assert offers.offer_login(key, SITE, None, "pw one 1") is True  # still noted for this site
    offers.confirm_offer(key)
    assert offers.pending_offer(key).username == "me@example.com"
    assert offers.offer_login(key, SITE, None, "pw one 1") is False  # used up


def test_a_click_or_failed_attempt_before_the_real_submit_keeps_the_noted_username(sessions):
    key = _key(sessions)
    saved_logins.save_login(site=SITE, username="me@example.com", password="pw old 0", saved_by=OWNER_USER)
    offers.note_username(key, SITE, "me@example.com")
    with pytest.raises(KeyStoreError):
        offers.offer_login(key, SITE, None, "")  # a show-password click with the field still empty
    assert offers.offer_login(key, SITE, None, "pw old 0") is False  # the saved password: no offer
    assert offers.offer_login(key, SITE, None, "pw wrong 9") is True  # a failed attempt, never confirmed
    assert offers.offer_login(key, SITE, None, "pw one 1") is True  # the real submit
    assert offers.confirm_offer(key) is True
    assert offers.pending_offer(key) == offers.OfferView(SITE, "example.com", "me@example.com", True)


def test_confirming_or_saving_uses_up_the_noted_username(sessions):
    key = _key(sessions)
    offers.note_username(key, SITE, "me@example.com")
    assert offers.offer_login(key, SITE, None, "pw one 1") is True
    assert offers._usernames  # kept until the sign-in is confirmed
    offers.confirm_offer(key)
    assert offers._usernames == {}
    offers.note_username(key, SITE, "me@example.com")  # noted again after the confirm
    offers.save_offer(key, saved_by=OWNER_USER)
    assert offers._usernames == {}
    assert offers.offer_login(key, SITE, None, "pw two 2") is False


def test_a_username_noted_for_one_site_is_never_used_on_another(sessions):
    key = _key(sessions)
    offers.note_username(key, SITE, "me@example.com")
    assert offers.offer_login(key, "https://other.example", None, "pw one 1") is False
    assert offers.offer_login(key, "https://other.example", "someone", "pw one 1") is True
    offers.confirm_offer(key)
    assert offers.pending_offer(key) == offers.OfferView("https://other.example", "other.example", "someone", False)


def test_a_login_already_saved_with_this_password_gets_no_offer(sessions):
    key = _key(sessions)
    saved_logins.save_login(site=SITE, username="me", password="pw one 1", saved_by=OWNER_USER)
    assert offers.offer_login(key, SITE, "me", "pw one 1") is False
    assert offers.confirm_offer(key) is False
    assert offers.offer_login(key, SITE, "me", "pw new 2") is True
    offers.confirm_offer(key)
    assert offers.pending_offer(key).update is True


def test_drop_offer_drops_either_state(sessions):
    key = _key(sessions)
    offers.offer_login(key, SITE, "me", "pw one 1")
    offers.drop_offer(key)
    assert offers.confirm_offer(key) is False
    _confirmed(key)
    offers.drop_offer(key)
    assert offers.pending_offer(key) is None
    offers.drop_offer(key)  # nothing left: still fine


def test_a_drop_while_an_offer_is_being_checked_wins(sessions, monkeypatch):
    key = _key(sessions)
    checking = offers.logins_for_site

    def dropped_meanwhile(site, *, db=None):
        offers.drop_offer(key)  # the tab left the site while the database was read
        return checking(site, db=db)

    monkeypatch.setattr(offers, "logins_for_site", dropped_meanwhile)
    assert offers.offer_login(key, SITE, "me", "pw one 1") is False
    assert offers.confirm_offer(key) is False
    assert offers._claims == {}


def test_closing_the_browser_drops_its_offers_and_only_its_own(sessions):
    sessions["t-other"] = "s9"
    mine, other = _key(sessions), _key(sessions, "t-other")
    _confirmed(mine)
    offers.note_username(mine, SITE, "me")
    _confirmed(other)
    browser_control.browser_closed("t-mine")
    assert offers._offers.keys() == {other}
    assert offers._usernames == {}


def test_an_offer_from_an_earlier_browser_session_is_not_shown(sessions):
    _confirmed(_key(sessions))
    assert offers.pending_offer(offers.session_key_for("t-mine")) is not None
    sessions["t-mine"] = "s2"
    assert offers.pending_offer(offers.session_key_for("t-mine")) is None
    del sessions["t-mine"]
    assert offers.session_key_for("t-mine") is None


def test_a_restart_keeps_no_offer(sessions):
    _confirmed(_key(sessions))
    reloaded = importlib.reload(offers)
    try:
        assert reloaded._offers == {} and reloaded._usernames == {}
    finally:
        importlib.reload(offers)


@pytest.mark.parametrize("site", [
    f"https://localhost:{DEFAULT_BACKEND_PORT}",
    f"https://127.0.0.1:{DEFAULT_BACKEND_PORT}",
    f"https://127.8.9.1:{DEFAULT_BACKEND_PORT}",
    f"https://[::1]:{DEFAULT_BACKEND_PORT}",
    f"https://[::ffff:127.0.0.1]:{DEFAULT_BACKEND_PORT}",
    f"https://0.0.0.0:{DEFAULT_BACKEND_PORT}",
    f"https://app.localhost:{DEFAULT_BACKEND_PORT}",
    "https://localhost:47901",
    "https://localhost:3005",
])
def test_corsias_own_address_is_refused(sessions, monkeypatch, site):
    monkeypatch.setenv("BACKEND_PORT", "3005")
    key = _key(sessions)
    _confirmed(key)
    with pytest.raises(KeyStoreError, match="CORSIA itself"):
        offers.offer_login(key, site, "me", "pw one 1")
    assert offers.pending_offer(key) is None


def test_this_computers_name_and_the_outside_address_are_refused(sessions):
    key = _key(sessions)
    with pytest.raises(KeyStoreError, match="CORSIA itself"):
        offers.offer_login(key, f"https://{local_hostname()}:{DEFAULT_BACKEND_PORT}", "me", "pw one 1")
    set_devices_outside_address("home.example.net:8443")
    with pytest.raises(KeyStoreError, match="CORSIA itself"):
        offers.offer_login(key, "https://HOME.example.net:8443/", "me", "pw one 1")
    assert offers.offer_login(key, "https://home.example.net", "me", "pw one 1") is True
    assert offers.offer_login(key, "https://localhost:8443", "me", "pw one 1") is True


@pytest.mark.parametrize(
    "stored,site", [("home.example.net.", "https://home.example.net"), ("home.example.net.:8443", "https://home.example.net:8443")],
)
def test_an_outside_address_saved_with_a_trailing_dot_is_refused(sessions, stored, site):
    set_devices_outside_address(stored)
    with pytest.raises(KeyStoreError, match="CORSIA itself"):
        offers.offer_login(_key(sessions), site, "me", "pw one 1")
    assert offers.pending_offer(_key(sessions)) is None


def test_an_outside_address_that_is_not_a_site_refuses_the_offer(sessions):
    set_devices_outside_address("not a host!")
    with pytest.raises(KeyStoreError):
        offers.offer_login(_key(sessions), SITE, "me", "pw one 1")
    offers.confirm_offer(_key(sessions))
    assert offers.pending_offer(_key(sessions)) is None


def test_a_malformed_session_key_is_refused(sessions):
    for key in ("", "t-mine", ":s1", None):
        with pytest.raises(KeyStoreError):
            offers.offer_login(key, SITE, "me", "pw one 1")


def _seam_modules() -> set[str]:
    pattern = re.compile(r"\b(" + "|".join(SEAM) + r")\b")
    return {
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if ".venv" not in path.parts and pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    }


def test_only_the_allowlisted_modules_can_make_or_confirm_an_offer():
    assert _seam_modules() <= SEAM_ALLOWLIST, (
        "A module outside the allowlist names the save-offer seam. The browser's capture of the person's "
        "own submit joins SEAM_ALLOWLIST on purpose, in its own reviewed change."
    )
    assert "infra/saved_login_offers.py" in _seam_modules()


# Routes


@pytest.mark.asyncio
async def test_get_answers_null_until_an_offer_is_confirmed_and_has_no_side_effects(client, sessions):
    empty = await client.get("/api/saved-logins/offer", params={"thread_id": "t-mine"}, headers=_person())
    assert empty.status_code == 200 and empty.json() == {"offer": None}
    offers.offer_login(_key(sessions), SITE, "me", "pw one 1")
    unconfirmed = await client.get("/api/saved-logins/offer", params={"thread_id": "t-mine"}, headers=_person())
    assert unconfirmed.json() == {"offer": None}
    offers.confirm_offer(_key(sessions))
    for _ in range(2):
        shown = await client.get("/api/saved-logins/offer", params={"thread_id": "t-mine"}, headers=_person())
        assert shown.status_code == 200
        assert shown.json() == {"offer": {"site": SITE, "domain": "example.com", "username": "me", "update": False}}


@pytest.mark.asyncio
async def test_get_answers_null_with_no_browser_open(client, sessions):
    del sessions["t-mine"]
    shown = await client.get("/api/saved-logins/offer", params={"thread_id": "t-mine"}, headers=_person())
    assert shown.status_code == 200 and shown.json() == {"offer": None}


@pytest.mark.asyncio
async def test_save_writes_the_login_as_the_person_and_drops_the_offer(client, sessions):
    _confirmed(_key(sessions), password="pw one 1")
    saved = await client.post("/api/saved-logins/offer/save", json={"thread_id": "t-mine"}, headers=_person())
    assert saved.status_code == 200 and saved.json() == {"saved": True}
    [login] = saved_logins.list_logins()
    assert (login.site, login.username, login.saved_by) == (SITE, "me@example.com", OWNER_USER)
    assert saved_logins.password_matches(login.id, "pw one 1")
    assert offers.pending_offer(_key(sessions)) is None
    again = await client.post("/api/saved-logins/offer/save", json={"thread_id": "t-mine"}, headers=_person())
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_save_refuses_an_unconfirmed_offer_and_keeps_it(client, sessions):
    offers.offer_login(_key(sessions), SITE, "me", "pw one 1")
    refused = await client.post("/api/saved-logins/offer/save", json={"thread_id": "t-mine"}, headers=_person())
    assert refused.status_code == 409
    assert saved_logins.list_logins() == []
    assert offers.confirm_offer(_key(sessions)) is True


@pytest.mark.asyncio
async def test_not_now_drops_the_offer_and_is_safe_to_repeat(client, sessions):
    _confirmed(_key(sessions))
    for _ in range(2):
        dropped = await client.post("/api/saved-logins/offer/not-now", json={"thread_id": "t-mine"}, headers=_person())
        assert dropped.status_code == 200
    assert offers.pending_offer(_key(sessions)) is None
    assert saved_logins.list_logins() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"[]", b"{}", b'{"thread_id": 3}', b"not json", b"\xff", b"{" * 2000])
async def test_a_malformed_body_is_a_fixed_refusal(client, sessions, body):
    refused = await client.post("/api/saved-logins/offer/save", content=body, headers=_person())
    assert refused.status_code == 400
    assert refused.json() == {"detail": "Couldn’t do that: the request didn’t name a valid chat. Send it again with the chat’s id."}


@pytest.mark.asyncio
async def test_another_persons_thread_is_not_found(client, sessions):
    sessions["t-other"] = "s9"
    _confirmed(_key(sessions, "t-other"))
    shown = await client.get("/api/saved-logins/offer", params={"thread_id": "t-other"}, headers=_person())
    saved = await client.post("/api/saved-logins/offer/save", json={"thread_id": "t-other"}, headers=_person())
    dropped = await client.post("/api/saved-logins/offer/not-now", json={"thread_id": "t-other"}, headers=_person())
    assert [shown.status_code, saved.status_code, dropped.status_code] == [404, 404, 404]
    assert offers.pending_offer(_key(sessions, "t-other")) is not None


@pytest.mark.asyncio
async def test_list_and_delete(client, sessions):
    login_id = saved_logins.save_login(site=SITE, username="me", password="pw one 1", saved_by=OWNER_USER)
    listed = await client.get("/api/saved-logins", headers=_person())
    assert listed.status_code == 200
    assert [(row["id"], row["site"], row["username"]) for row in listed.json()["logins"]] == [(login_id, SITE, "me")]
    assert "pw one 1" not in listed.text
    deleted = await client.delete(f"/api/saved-logins/{login_id}", headers=_person())
    assert deleted.status_code == 200 and deleted.json() == {"deleted": login_id}
    assert saved_logins.list_logins() == []
    missing = await client.delete(f"/api/saved-logins/{login_id}", headers=_person())
    assert missing.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("login_id", ["0" * 32, "not-an-id"])
async def test_deleting_a_login_that_is_not_there_says_so(client, sessions, login_id):
    missing = await client.delete(f"/api/saved-logins/{login_id}", headers=_person())
    assert missing.status_code == 404
    assert missing.json() == {"detail": "There's no saved login to delete."}


_ROUTES = [
    ("GET", "/api/saved-logins/offer?thread_id=t-mine", None),
    ("POST", "/api/saved-logins/offer/save", {"thread_id": "t-mine"}),
    ("POST", "/api/saved-logins/offer/not-now", {"thread_id": "t-mine"}),
    ("GET", "/api/saved-logins", None),
    ("DELETE", "/api/saved-logins/" + "0" * 32, None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _ROUTES)
async def test_an_agent_token_is_refused_on_every_route(client, sessions, method, path, body):
    _confirmed(_key(sessions))
    login_id = saved_logins.save_login(site=SITE, username="kept", password="pw two 2", saved_by=OWNER_USER)
    for headers in (_person(OWNER_TOKEN), {}):
        refused = await client.request(method, path.replace("0" * 32, login_id), json=body, headers=headers)
        assert refused.status_code in {401, 403}, (method, path, refused.status_code)
        assert "pw one 1" not in refused.text and "me@example.com" not in refused.text
    assert offers.pending_offer(_key(sessions)) is not None
    assert [login.id for login in saved_logins.list_logins()] == [login_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _ROUTES)
async def test_a_session_in_the_url_reads_but_changes_nothing(client, sessions, method, path, body):
    _confirmed(_key(sessions))
    login_id = saved_logins.save_login(site=SITE, username="kept", password="pw two 2", saved_by=OWNER_USER)
    route, _, query = path.replace("0" * 32, login_id).partition("?")
    url_session = dict(parse_qsl(query)) | {"token": OWNER_SESSION_TOKEN}
    answered = await client.request(method, route, json=body, params=url_session)
    assert answered.status_code == (200 if method == "GET" else 401), (method, path, answered.status_code)
    assert offers.pending_offer(_key(sessions)) is not None
    assert [login.id for login in saved_logins.list_logins()] == [login_id]


def test_every_route_is_the_persons_alone():
    from infra.auth import require_human_user

    routes = [route for route in saved_logins_api.router.routes if route.path.startswith("/api/saved-logins")]
    assert len(routes) == len(_ROUTES)
    for route in routes:
        expected = require_human_user if route.methods == {"GET"} else saved_logins_api.require_person_write
        assert any(dep.call is expected for dep in route.dependant.dependencies), route.path
