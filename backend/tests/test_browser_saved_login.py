"""browser_fill's saved-login path: what an agent may name, what it is told, and that the password never shows.

The page and browser session are the fill executor tests' fakes (test_browser_login_fill.world); the approval,
the saved login and the result functions are real, in an isolated database.
"""
from __future__ import annotations

import logging

import pytest

from host_access import browser_login_fill as fill
from infra import product_events, saved_login_fills, saved_logins
from infra.database import get_db
from test_browser_login_fill import SITE, USER, world  # noqa: F401 - the fixture
import tools.browser_saved_login as choice
from tools.browser_actions import BrowserFillTool

THREAD = "thread-fill"
PERSON = "person"
REAL_MAY_FILL = saved_login_fills.may_fill


@pytest.fixture
def agent(world, monkeypatch):  # noqa: F811
    """The world's login, owned by the person whose agent thread calls, with the real grant check."""
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
               (THREAD, PERSON, "Bookkeeper", "t", "t"))
    db.commit()
    db.close()
    monkeypatch.setattr(saved_login_fills, "may_fill", REAL_MAY_FILL)
    monkeypatch.setattr(saved_login_fills, "_approvals", {})
    world.page_url = f"{SITE}/login"

    async def page_url(_thread_id):
        return world.page_url

    monkeypatch.setattr(choice, "_page_url", page_url)
    world.events = []
    monkeypatch.setattr(product_events, "emit_product_event",
                        lambda *args, **kwargs: world.events.append(repr((args, kwargs))))
    return world


async def _fill(**arguments) -> str:
    return await BrowserFillTool().execute(_thread_id=THREAD, **arguments)


def _requests() -> list[dict]:
    return saved_login_fills.listed(PERSON)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"selector": "@e1"}, {"text": "x"}, {"selector": "@e1", "text": "x"}])
async def test_saved_login_with_a_selector_or_text_is_refused(agent, extra):
    assert await _fill(saved_login={"site": SITE, "username": USER}, **extra) == choice.EXCLUSIVE
    assert _requests() == [] and agent.typed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, -1, 31, 1.5, True, False, "30", "until_revoked"])
async def test_minutes_outside_one_to_thirty_or_not_a_whole_number_is_refused_never_clamped(agent, minutes):
    assert await _fill(saved_login={"site": SITE, "username": USER}, minutes=minutes) == choice.BAD_MINUTES
    assert _requests() == [] and agent.typed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_login", ["x", {}, {"site": SITE}, {"username": USER}, {"site": 1, "username": USER}])
async def test_a_saved_login_without_a_site_and_username_is_refused(agent, saved_login):
    assert await _fill(saved_login=saved_login) == choice.BAD_LOGIN


@pytest.mark.asyncio
async def test_a_request_asks_the_person_at_once_with_the_minutes_asked_for_and_fills_nothing(agent):
    text = await _fill(saved_login={"site": f"{SITE}/login?next=1", "username": USER}, minutes=20)
    assert text == fill.ASKED.format(site=SITE, username=USER)
    [request] = _requests()
    assert (request["state"], request["thread_id"], request["requested_duration_seconds"]) == (
        saved_login_fills.AWAITING_USER, THREAD, 1200)
    assert request["standing"] is False and agent.typed == []


@pytest.mark.asyncio
async def test_once_the_person_approves_the_same_call_fills_through_the_executor(agent, caplog):
    await _fill(saved_login={"site": SITE, "username": USER})
    [request] = _requests()
    saved_login_fills.approve_fill(request["id"], user_id=PERSON, revision=request["revision"],
                                   application_hash=request["application_hash"], duration_seconds=300)
    text = await _fill(saved_login={"site": SITE, "username": USER}, minutes=5)
    assert agent.typed == [(USER, agent.sentinel)]
    assert text not in {fill.ASKED.format(site=SITE, username=USER), choice.BAD_MINUTES}
    _never_shown(agent, caplog, text)


@pytest.mark.asyncio
async def test_a_login_another_person_saved_answers_exactly_as_one_that_doesnt_exist(agent):
    saved_logins.save_login(site=SITE, username="other@example.com", password="Another-secret-1", saved_by="someone")
    theirs = await _fill(saved_login={"site": SITE, "username": "other@example.com"})
    missing = await _fill(saved_login={"site": SITE, "username": "nobody@example.com"})
    assert theirs == missing == choice.NO_MATCH + choice.ON_PAGE.format(usernames=USER)
    assert _requests() == []


@pytest.mark.asyncio
async def test_a_grant_for_a_login_now_saved_by_someone_else_fills_nothing(agent):
    await _fill(saved_login={"site": SITE, "username": USER})
    [request] = _requests()
    saved_login_fills.approve_fill(request["id"], user_id=PERSON, revision=request["revision"],
                                   application_hash=request["application_hash"], duration_seconds=300)
    db = get_db()
    db.execute("UPDATE saved_logins SET saved_by = 'someone' WHERE id = ?", (agent.login_id,))
    db.commit()
    db.close()
    assert saved_login_fills.may_fill(THREAD, agent.login_id) is False
    assert agent.typed == []


@pytest.mark.parametrize("out_of_date", [False, True])
def test_a_request_for_another_persons_login_is_refused_on_the_server(agent, out_of_date):
    theirs = saved_logins.save_login(site=SITE, username="other@example.com", password="Another-secret-1",
                                     saved_by="someone")
    if out_of_date:
        saved_logins.mark_out_of_date(theirs)
    with pytest.raises(saved_login_fills.FillApprovalError) as refused:
        saved_login_fills.request_fill(THREAD, theirs, requested_seconds=60)
    # Ownership is checked first, so the refusal names neither their site nor their username.
    assert SITE not in str(refused.value) and "other@example.com" not in str(refused.value)
    assert _requests() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("page_url", [None, "http://example.com/login", "https://elsewhere.example/login", "about:blank"])
async def test_the_list_covers_only_the_current_pages_site(agent, page_url):
    agent.page_url = page_url
    text = await _fill(saved_login={"site": SITE, "username": "nobody@example.com"})
    assert text == choice.NO_MATCH + choice.NONE_ON_PAGE


@pytest.mark.asyncio
async def test_the_list_names_usernames_only_never_an_id_status_or_password(agent, caplog):
    saved_logins.mark_out_of_date(agent.login_id)
    text = await _fill(saved_login={"site": SITE, "username": "nobody@example.com"})
    assert text == choice.NO_MATCH + choice.ON_PAGE.format(usernames=USER)
    assert agent.login_id not in text and "date" not in text
    _never_shown(agent, caplog, text)


def _never_shown(w, caplog, *texts: str) -> None:
    """The saved value and its length are in no result, log record, event or person-step instruction."""
    caplog.set_level(logging.DEBUG)
    held = [caplog.text, *texts, *w.owner.declared, *w.events]
    assert not any(w.sentinel in text for text in held)
    assert not any(f"{len(w.sentinel)} char" in text for text in held)
