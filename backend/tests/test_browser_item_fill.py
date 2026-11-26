"""The saved-item fill, end to end on a stub page: the approval, the clean-page and field checks, the fill and its
audit, with only the browser session and the page's CDP answers faked.

The approvals, the item and its value are real (an isolated database and values file); the page half
(browser_login_page.item_field, fill_item) runs as written against a stub CDP session, so every refusal comes
from the same checks a real page meets. Real Chrome runs the page scripts in test_browser_login_fill_e2e.py.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest

import api.provider_meta  # noqa: F401 - the item list reads it; imported before the fixture stubs CdpClient
import infra.saved_login_fills as fills
from auth_fixtures import OWNER_USER as PERSON
from host_access import (
    browser_cdp, browser_dom_write, browser_fill_marks, browser_item_fill as item_fill, browser_login_fill,
    browser_login_page as page, browser_saved_fills, browser_session_commands,
)
from host_access.browser_dom_hit import Located
from host_access.browser_session_state import BrowserSessionError
from infra import key_store, saved_item_fills
from infra.database import get_db
from tools import browser_saved_login

THREAD = "item-fill-thread"
SITE = "https://portal.example.org"
NAME = "member-number"
LABEL = "Member number"


class Owner:
    def __init__(self) -> None:
        self.runtime = SimpleNamespace(action_lock=asyncio.Lock(), record=SimpleNamespace(session="s1"))
        self.admit: BaseException | None = None

    async def _pin_native(self) -> None:
        return None

    async def _admit(self, _thread_id, **_kwargs):
        if self.admit is not None:
            raise self.admit
        return self.runtime


class Cdp:
    """The page's session: its frame tree, our isolated world, the located field and its two scripts."""

    def __init__(self, w) -> None:
        self.w = w

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def call(self, method, **params):
        w = self.w
        if method == "Page.getFrameTree":
            loader = w.loaders.pop(0) if len(w.loaders) > 1 else w.loaders[0]
            return {"frameTree": {"frame": {"id": "F1", "loaderId": loader, "securityOrigin": w.origin}}}
        if method == "Page.createIsolatedWorld":
            assert params["frameId"] == "F1" and params["grantUniveralAccess"] is False
            return {"executionContextId": 9}
        if method == "DOM.resolveNode":
            assert params["executionContextId"] == 9
            return {"object": {"objectId": "field-object"}}
        assert method == "Runtime.callFunctionOn" and params["objectId"] == "field-object"
        if params["functionDeclaration"] == page._ITEM_CHECK:
            w.steps.append("check")
            return {"result": {"value": w.field}}
        assert params["functionDeclaration"] == page._ITEM_FILL
        w.steps.append("fill")
        if w.fill_error is not None:
            raise w.fill_error
        w.filled.append([argument["value"] for argument in params["arguments"]])
        return {"result": {"value": w.fill_status}}


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch, caplog):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(fills, "_approvals", {})
    from infra import product_events
    from infra.state import ws

    w = SimpleNamespace(owner=Owner(), steps=[], filled=[], seen=[], loaders=["L1"], origin=SITE, located=True,
                        field={"field": True, "kind": "text", "form": True, "action": f"{SITE}/save", "method": "post"},
                        fill_status="typed", fill_error=None, linked=False, sentinel=f"Item-{uuid.uuid4().hex}")
    monkeypatch.setattr(ws, "broadcast_to_user", lambda *args, **kwargs: w.seen.append(repr(args)))
    monkeypatch.setattr(product_events, "emit_product_event", lambda *a, **k: w.seen.append(repr((a, k))))
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               (THREAD, PERSON, "Bookkeeper", None, "t", "t"))
    db.commit()
    db.close()
    key_store.add_entry(NAME, LABEL, "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value(NAME, w.sentinel)
    caplog.set_level(logging.DEBUG)
    _clear_marks()
    cdp = Cdp(w)

    async def endpoint(_command):
        return "ws://127.0.0.1:9/devtools/page/T1"

    async def browser(_command, _refusal):
        return "ws://127.0.0.1:9/devtools/browser/B"

    async def command(_owner, _runtime, _lease, args, _timeout):
        w.steps.append(args[0])

    async def locate(_run, _page, selector):
        w.steps.append(f"locate {selector}")
        return Located(5, cdp, None) if w.located else None

    async def linked(_cdp, _target):
        return w.linked

    real_mark = browser_saved_fills.mark_saved_fill
    monkeypatch.setattr(browser_saved_fills, "mark_saved_fill", lambda *a: (w.steps.append("mark"), real_mark(*a)))
    monkeypatch.setattr(browser_login_fill, "_owner", lambda: w.owner)
    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "browser_endpoint", browser)
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _endpoint: cdp)
    monkeypatch.setattr(browser_session_commands, "_command", command)
    monkeypatch.setattr(browser_dom_write, "locate_target", locate)
    monkeypatch.setattr(page, "linked_windows", linked)
    yield w
    _clear_marks()


def _clear_marks() -> None:
    for held in (browser_fill_marks._scripted, browser_fill_marks._session_marks, browser_fill_marks._site_marks,
                 browser_saved_fills._saved_fills):
        held.clear()


def _grant(seconds=600):
    row = saved_item_fills.request_item_fill(THREAD, NAME, SITE)
    return fills.approve_fill(row["id"], user_id=PERSON, revision=row["revision"],
                              application_hash=row["application_hash"], duration_seconds=seconds)


def _audit() -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute("SELECT * FROM saved_login_fill_audit").fetchall()]
    finally:
        db.close()


def _never_shown(w, caplog, *texts) -> None:
    held = [caplog.text, *w.seen, *(repr(text) for text in texts), repr(_audit()), repr(fills._approvals),
            *(repr(vars(module)) for module in (item_fill, page, browser_login_fill, browser_fill_marks))]
    assert not any(w.sentinel in text for text in held)


def _line(status: str) -> str:
    return item_fill.LINES[status].format(label=LABEL, site=SITE)


@pytest.mark.asyncio
async def test_without_a_grant_the_person_is_asked_for_the_field_site_and_nothing_is_read(world, caplog):
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5", requested_seconds=600)
    assert text == item_fill.ASKED.format(label=LABEL, site=SITE)
    [row] = fills.listed(PERSON)
    assert row["site"] == SITE and row["item_label"] == LABEL and row["requested_duration_seconds"] == 600
    assert "check" not in world.steps and "fill" not in world.steps and not world.filled
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_an_item_fills_end_to_end_under_its_grant_and_is_audited(world, caplog):
    _grant()
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5")
    assert text == item_fill.FILLED.format(label=LABEL, site=SITE)
    assert world.steps == ["locate @e5", "check", "mark", "fill"]
    assert world.filled == [[world.sentinel, "text", f"{SITE}/save", "post"]]
    assert browser_saved_fills.saved_fill_document(THREAD, "s1", "L1")
    [row] = _audit()
    assert (row["item"], row["login_id"], row["site"], row["thread_id"], row["result"]) == (NAME, "", SITE, THREAD,
                                                                                         "filled")
    world.filled.clear()  # the stub page's own copy; everything else must never have held it
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "status"), [
    ({"origin": "https://other.example.org"}, "frame_origin"),
    ({"origin": "http://portal.example.org"}, "http_frame"),
    ({"field": {"field": False}}, "not_field"),
    ({"field": {"field": True, "kind": "text", "form": True, "action": f"{SITE}/s", "method": "get"}}, "get_form"),
    ({"field": {"field": True, "kind": "text", "form": True, "action": "https://collect.example.net/p",
                "method": "post"}}, "form_action"),
    ({"field": {"field": True, "kind": "password", "form": True, "action": "http://portal.example.org/s",
                "method": "post"}}, "insecure_action"),
    ({"linked": True}, "not_clean"),
    ({"loaders": ["L1", "L2"]}, "changed"),
])
async def test_every_refusal_leaves_the_value_unread_and_the_page_unfilled(world, caplog, change, status):
    _grant()
    for key, value in change.items():
        setattr(world, key, value)
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5")
    assert text == _line(status)
    assert "fill" not in world.steps and "mark" not in world.steps and _audit() == []
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_an_agent_marked_session_is_not_clean(world, caplog):
    _grant()
    browser_fill_marks._session_marks.add((THREAD, "s1"))
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5")
    assert text == _line("not_clean") and "check" not in world.steps


@pytest.mark.asyncio
async def test_no_field_at_the_selector_and_no_browser_each_fill_nothing(world, caplog):
    _grant()
    world.located = False
    assert await item_fill.fill_saved_item(THREAD, NAME, "#missing") == _line("no_field")
    world.owner.admit = BrowserSessionError("none")
    assert await item_fill.fill_saved_item(THREAD, NAME, "@e5") == browser_login_fill.NO_BROWSER
    assert not world.filled and _audit() == []


@pytest.mark.asyncio
async def test_a_grant_that_ends_before_the_read_fills_nothing(world, caplog, monkeypatch):
    _grant()
    answers = [SITE, None]
    monkeypatch.setattr(saved_item_fills, "granted_site", lambda *_a: answers.pop(0))
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5")
    assert text == _line("no_grant") and "fill" not in world.steps and _audit() == []


@pytest.mark.asyncio
async def test_a_failed_fill_is_uncertain_audited_unknown_and_logs_only_its_type(world, caplog):
    _grant()
    world.fill_error = RuntimeError(world.sentinel)
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5")
    assert text == _line("uncertain")
    assert [row["result"] for row in _audit()] == ["unknown"]
    assert "RuntimeError" in caplog.text
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_a_field_that_changed_before_the_fill_is_not_audited(world, caplog):
    _grant()
    world.fill_status = "changed"
    assert await item_fill.fill_saved_item(THREAD, NAME, "@e5") == _line("changed")
    assert _audit() == []


@pytest.mark.asyncio
async def test_clearing_the_entry_ends_the_grant_so_the_next_fill_asks_again(world, caplog):
    _grant()
    key_store.clear_value(NAME)
    text = await item_fill.fill_saved_item(THREAD, NAME, "@e5")
    assert "has no value yet" in text and "Settings › Keys" in text
    assert "fill" not in world.steps and fills.listed(PERSON) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["server-box", "google-client-id", "saved-login-" + "0" * 32, "nope", 7])
async def test_the_tool_refuses_an_ineligible_name_and_lists_names_and_labels_only(world, caplog, name):
    text = await browser_saved_login.fill_item(THREAD, name, "@e5", None)
    assert text.startswith("No saved item has that name. Saved items you may ask for: ")
    assert f'{NAME} ("{LABEL}")' in text and 'github-webhook ("GitHub webhook secret")' in text
    assert "locate @e5" not in world.steps
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_the_tool_takes_a_selector_and_no_text_and_never_an_open_ended_time(world):
    from tools.browser_actions import BrowserFillTool

    tool = BrowserFillTool()
    context = {"_thread_id": THREAD}
    for kwargs in ({"saved_item": NAME}, {"saved_item": NAME, "selector": "@e5", "text": "x"},
                   {"saved_item": NAME, "selector": "@e5", "saved_login": {"site": SITE, "username": "u"}}):
        assert await tool.execute(**kwargs, **context) == browser_saved_login.ITEM_EXCLUSIVE
    for minutes in (0, 31, True, "5", 10**6):
        text = await tool.execute(saved_item=NAME, selector="@e5", minutes=minutes, **context)
        assert text == browser_saved_login.BAD_MINUTES
    assert fills.listed(PERSON) == [] and "locate @e5" not in world.steps
    text = await tool.execute(saved_item=NAME, selector="@e5", minutes=10, **context)
    assert text == item_fill.ASKED.format(label=LABEL, site=SITE)
    assert fills.listed(PERSON)[0]["requested_duration_seconds"] == 600
