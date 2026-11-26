"""The saved items a script declares: what may be named, the checks for its owner at registration and run start,
and that a script naming one always goes to the person.

The items and logins are real (an isolated database and values file); only the reviewer and the sandboxed process
are faked.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import infra.saved_logins as saved_logins
from auth_fixtures import OTHER_USER as OTHER_PERSON, OWNER_USER as PERSON
from automation import script_access, script_items, script_runner
from automation import script_schedules as store
from automation.script_readings import ITEMS_WITHHELD
from automation.script_sandbox import ScriptRun
from host_access.review_models import ReviewerIdentity, ScriptApplicationReview
from infra import key_store, state
from infra.database import get_db

OWNER = "items-owner"
OTHER = "items-other"
NAME = "billing-key"
VALUE = "Fake-billing-value-0123456789"  # gitleaks:allow - a test fixture, not a credential
PASSWORD = "Fake-login-password-0123456789"  # gitleaks:allow - a test fixture, not a credential


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    db = get_db()
    db.executemany("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                   [(OWNER, PERSON, "Bookkeeper", None, "t", "t"), (OTHER, OTHER_PERSON, "Theirs", None, "t", "t")])
    db.commit()
    db.close()
    key_store.add_entry(NAME, "Billing key", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value(NAME, VALUE)
    login = saved_logins.save_login(site="https://portal.example.org", username="me", password=PASSWORD,
                                    saved_by=PERSON)
    theirs = saved_logins.save_login(site="https://portal.example.net", username="them", password=PASSWORD,
                                     saved_by=OTHER_PERSON)
    scripts = tmp_path / "scripts"  # a folder of its own: the state folder beside it is not the script's
    scripts.mkdir()
    return SimpleNamespace(login=login, theirs=theirs, tmp=scripts)


def _declared(raw, thread=OWNER, authenticated=True):
    return script_items.declared(thread, raw, authenticated=authenticated)


def test_items_and_logins_are_declared_with_their_place(world):
    entries = _declared([{"login": world.login}, {"name": NAME, "place": "api.example.com"}])
    assert entries == [
        {"name": NAME, "label": "Billing key", "place": "api.example.com"},
        {"login": world.login, "label": "Saved login for portal.example.org (me)", "place": "portal.example.org"},
    ]
    assert _declared([{"name": NAME, "given": True}]) == [{"name": NAME, "label": "Billing key", "given": True}]
    assert _declared(None) == [] and _declared([]) == []


def test_a_login_goes_only_to_its_own_site(world):
    assert _declared([{"login": world.login, "place": "portal.example.org"}])[0]["place"] == "portal.example.org"
    with pytest.raises(ValueError, match="only to the site it was saved for, portal.example.org"):
        _declared([{"login": world.login, "place": "evil.example.com"}])
    with pytest.raises(ValueError, match="never given to a script"):
        _declared([{"login": world.login, "given": True}])
    odd_port = saved_logins.save_login(site="https://portal.example.org:8443", username="me", password=PASSWORD,
                                       saved_by=PERSON)
    with pytest.raises(ValueError, match="port other than 443"):
        _declared([{"login": odd_port}])


@pytest.mark.parametrize("place", ["https://api.example.com", "api.example.com:443", "api.example.com/v1",
                                   "127.0.0.1", "localhost", "API.example.com", "api.example.com.", "", 7,
                                   "0x7f.1", "0x7f000001", "10.0x1"])
def test_a_place_is_a_plain_lowercase_host(world, place):
    with pytest.raises(ValueError, match="place is a host name"):
        _declared([{"name": NAME, "place": place}])


@pytest.mark.parametrize("raw,match", [
    ([{"name": NAME}], "either a place"),
    ([{"name": NAME, "place": "a.example.com", "given": True}], "either a place"),
    ([{"name": NAME, "given": False}], "either a place"),
    ([{"name": NAME, "login": "x"}], "saved_items is a list"),
    ([{"name": NAME, "place": "a.example.com", "extra": 1}], "saved_items is a list"),
    (["billing-key"], "saved_items is a list"),
    ([{"name": NAME, "place": "a.example.com"}, {"name": NAME, "given": True}], "names \"billing-key\" twice"),
])
def test_the_list_shape_is_checked(world, raw, match):
    with pytest.raises(ValueError, match=match):
        _declared(raw)


def test_saved_items_need_an_authenticated_script(world):
    with pytest.raises(ValueError, match="needs authenticated=true"):
        _declared([{"name": NAME, "place": "a.example.com"}], authenticated=False)


def test_an_item_outside_the_owners_scope_is_refused_at_registration(world):
    key_store.add_entry("unset-key", "Unset key", "", added_by_thread_id=None, added_by_name=None)
    with pytest.raises(ValueError, match="has no value yet"):
        _declared([{"name": "unset-key", "place": "a.example.com"}])
    # A code-owned (managed or connected-app) entry is not a saved item an agent may use.
    key_store.ensure_managed_entry("server-relay", "Relay key")
    key_store.set_managed_value("server-relay", VALUE)
    with pytest.raises(ValueError, match="No saved item has that name"):
        _declared([{"name": "server-relay", "place": "a.example.com"}])
    with pytest.raises(ValueError, match="No saved item has that name"):
        _declared([{"name": "no-such-item", "place": "a.example.com"}])
    # Another person's saved login.
    with pytest.raises(ValueError, match="No saved login you may use"):
        _declared([{"login": world.theirs}])
    # Another thread than the running turn's own.
    token = state._current_thread_id.set(OTHER)
    try:
        with pytest.raises(ValueError, match="only be made for the agent's own thread"):
            _declared([{"name": NAME, "place": "a.example.com"}])
    finally:
        state._current_thread_id.reset(token)


def _run_items(world, entries):
    auth = {"saved_items": _declared(entries)}
    return script_items.for_run(OWNER, "schedule-1", auth, None), auth


def test_setting_a_value_again_keeps_the_approval_and_clearing_or_removing_ends_it(world):
    items, auth = _run_items(world, [{"name": NAME, "place": "a.example.com"}])
    assert items is not None and not items.withheld and items.sendable()[0].name == NAME
    key_store.set_value(NAME, VALUE + "-again")
    assert script_items.for_run(OWNER, "schedule-1", auth, None) is not None
    key_store.clear_value(NAME)
    with pytest.raises(script_items.ItemsUnavailable):
        script_items.for_run(OWNER, "schedule-1", auth, None)
    key_store.set_value(NAME, VALUE)
    key_store.remove_entry(NAME)
    with pytest.raises(script_items.ItemsUnavailable):
        script_items.for_run(OWNER, "schedule-1", auth, None)


def test_a_login_deleted_or_out_of_date_ends_it(world):
    _items, auth = _run_items(world, [{"login": world.login}])
    saved_logins.mark_out_of_date(world.login)
    with pytest.raises(script_items.ItemsUnavailable):
        script_items.for_run(OWNER, "schedule-1", auth, None)
    saved_logins.delete_login(world.login)
    with pytest.raises(script_items.ItemsUnavailable):
        script_items.for_run(OWNER, "schedule-1", auth, None)


def test_a_rule_the_approval_does_not_name_withholds_the_items(world):
    _items, auth = _run_items(world, [{"name": NAME, "place": "a.example.com"}])
    items = script_items.for_run(OWNER, "schedule-1", {**auth, "rules": []}, "rule-1")
    assert items is not None and items.withheld
    assert script_items.for_run(OWNER, "schedule-1", auth, None).withheld is False  # a cadence run isn't a rule run


def _reviewed(monkeypatch, tmp_path, *, auto=True, verdict="allow"):
    script = tmp_path / "task.py"
    script.write_text("print('ok')\n")
    sha = hashlib.sha256(script.read_bytes()).hexdigest()
    monkeypatch.setattr(script_access, "pin_script", lambda *args, **kwargs: (script, sha))
    monkeypatch.setattr(script_access, "_read_denial", lambda *_args: None)
    monkeypatch.setattr(script_access, "get_host_access_auto_approve", lambda: auto)
    monkeypatch.setattr(script_access.reviewer_target, "resolve_reviewer_target",
                        AsyncMock(return_value=SimpleNamespace(harness="native")))
    identity = ReviewerIdentity("test", "native", "test", "test", "test")
    reviewer = AsyncMock(return_value=ScriptApplicationReview(identity, verdict, "reason"))
    monkeypatch.setattr(script_access, "review_application", reviewer)
    monkeypatch.setattr(script_access, "deliver_to_thread", AsyncMock(return_value=True))
    return {"script_path": str(script), "purpose": "work", "user_request": "please do work"}, reviewer


@pytest.mark.asyncio
async def test_a_script_naming_saved_items_goes_to_the_person_even_under_auto_approve(world, monkeypatch):
    params, reviewer = _reviewed(monkeypatch, world.tmp)
    _path, _sha, plain = await script_access.review_script(OWNER, params)
    assert plain["state"] == "approved" and "saved_items" not in plain
    named = {**params, "saved_items": [{"name": NAME, "place": "api.example.com"}]}
    _path, _sha, record = await script_access.review_script(OWNER, named)
    assert record["state"] == "pending"
    assert record["saved_items"] == [{"name": NAME, "label": "Billing key", "place": "api.example.com"}]
    assert reviewer.await_args.args[1]["saved_items"] == record["saved_items"]  # the reviewer sees them
    assert record["application_hash"] != plain["application_hash"]  # and they are inside the approved hash
    moved = {**params, "saved_items": [{"name": NAME, "place": "other.example.com"}]}
    _path, _sha, changed = await script_access.review_script(OWNER, moved)
    assert changed["application_hash"] != record["application_hash"]


# The run's start, through the runner: a removed or cleared item pauses the script as a changed file does.

def _approved_schedule(world, saved):
    from automation import script_tree

    path = world.tmp / "run.py"
    path.write_text("print('x')\n")
    db = get_db()
    try:
        schedule = store.create_script(OWNER, "__workspace__", script_path=path, script_sha256=store.file_sha256(path),
                                       cadence_seconds=600, series=[], timeout_seconds=30,
                                       now="2026-01-01T00:00:00+00:00", db=db)
        db.commit()
    finally:
        db.close()
    auth = {"state": "approved", "tree_sha256": script_tree.tree_sha256(script_tree.read_tree(path)),
            "saved_items": saved}
    return replace(schedule, payload={**(schedule.payload or {}), "authentication": auth})


@pytest.mark.asyncio
async def test_run_start_gives_the_items_and_pauses_when_one_is_cleared(world, monkeypatch):
    calls = []

    async def fake(thread_id, project_id, path, *, run_id, timeout_seconds, authenticated=False, saved_items=None):
        calls.append(saved_items)
        return ScriptRun(0, b"", b"", False)

    monkeypatch.setattr(script_runner, "run_script", fake)
    schedule = _approved_schedule(world, _declared([{"name": NAME, "place": "api.example.com"}]))
    report = await script_runner._execute(schedule)
    assert report.status == "ok" and report.withheld is None
    assert calls[0].owner == OWNER and [use.name for use in calls[0].sendable()] == [NAME]
    key_store.clear_value(NAME)
    report = await script_runner._execute(schedule)
    assert report.status == "hash_mismatch" and len(calls) == 1
    assert "a saved item it uses was removed, its value was cleared" in (report.error or "")


def test_the_withheld_note_says_why():
    assert ITEMS_WITHHELD.startswith("Saved items were withheld from this run")
