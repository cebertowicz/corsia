"""A saved login fills only under the person's live, timed grant for exactly that agent and login."""
from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import infra.saved_login_fills as fills
import infra.saved_logins as saved_logins
from auth_fixtures import OTHER_USER as OTHER_PERSON, OWNER_USER as PERSON
from host_access.decision_capability import INTERACTIVE_UI_HEADER, USER_DECISION_HEADER
from infra.database import get_db

AGENT_THREAD = "agent-thread"
OTHER_THREAD = "other-thread"


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch):
    """Two agent threads, one subagent, two saved logins, and no approvals in memory."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(fills, "_approvals", {})
    from infra.state import ws

    nudges: list[tuple] = []
    monkeypatch.setattr(ws, "broadcast_to_user", lambda *args, **kwargs: nudges.append(args))
    db = get_db()
    db.executemany(
        "INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        [
            (AGENT_THREAD, PERSON, "Bookkeeper", None, "t", "t"),
            (OTHER_THREAD, PERSON, "Researcher", None, "t", "t"),
            ("worker-thread", PERSON, "", AGENT_THREAD, "t", "t"),
            ("someone-else", OTHER_PERSON, "Their agent", None, "t", "t"),
        ],
    )
    db.commit()
    db.close()
    first = saved_logins.save_login(
        site="https://example.com", username="me@example.com", password="Correct horse 1", saved_by=PERSON,
    )
    second = saved_logins.save_login(
        site="https://example.org", username="me", password="Battery staple 2", saved_by=PERSON,
    )
    return {"login": first, "other_login": second, "nudges": nudges}


def _approve(row, seconds=600, user_id=PERSON):
    return fills.approve_fill(
        row["id"], user_id=user_id, revision=row["revision"], application_hash=row["application_hash"],
        duration_seconds=seconds,
    )


def test_a_request_names_the_agent_site_username_and_time_from_server_records(world):
    row = fills.request_fill(AGENT_THREAD, world["login"])
    assert row["kind"] == "fill" and row["state"] == "awaiting_user"
    assert row["agent"] == "Bookkeeper"
    assert row["site"] == "https://example.com" and row["username"] == "me@example.com"
    assert row["created_at"] and row["expires_at"] is None and row["duration_seconds"] is None
    assert row["max_duration_seconds"] == 1800
    assert row["root_thread_id"] == AGENT_THREAD
    user_id, message = world["nudges"][-1]
    assert user_id == PERSON and message["type"] == "host_access_changed"
    assert message["lease_id"] == row["id"] and AGENT_THREAD in message["thread_ids"]


def test_a_subagent_request_shows_in_its_root_chat_with_a_name_even_untitled(world):
    row = fills.request_fill("worker-thread", world["login"])
    assert row["root_thread_id"] == AGENT_THREAD
    assert row["agent"] == "An agent"
    assert [item["id"] for item in fills.listed(PERSON, AGENT_THREAD)] == [row["id"]]
    assert fills.listed(OTHER_PERSON) == []


def test_duplicate_requests_for_one_thread_and_login_collapse(world):
    first = fills.request_fill(AGENT_THREAD, world["login"])
    assert fills.request_fill(AGENT_THREAD, world["login"])["id"] == first["id"]
    other = fills.request_fill(AGENT_THREAD, world["other_login"])
    another = fills.request_fill(OTHER_THREAD, world["login"])
    assert len({first["id"], other["id"], another["id"]}) == 3
    granted = _approve(first)
    assert fills.request_fill(AGENT_THREAD, world["login"])["id"] == granted["id"]
    assert len(fills.listed(PERSON)) == 3


def test_unknown_thread_or_login_is_refused(world):
    with pytest.raises(fills.FillApprovalError):
        fills.request_fill("no-such-thread", world["login"])
    with pytest.raises(fills.FillApprovalError):
        fills.request_fill(AGENT_THREAD, "0" * 32)
    with pytest.raises(fills.FillApprovalError):
        fills.request_fill("", world["login"])
    assert fills.listed(PERSON) == []


def test_may_fill_only_under_a_live_grant_for_exactly_that_thread_and_login(world, monkeypatch):
    login = world["login"]
    row = fills.request_fill(AGENT_THREAD, login)
    assert fills.may_fill(AGENT_THREAD, login) is False  # before approval
    granted = _approve(row, seconds=60)
    assert granted["state"] == "active" and granted["duration_seconds"] == 60 and granted["expires_at"]
    assert fills.may_fill(AGENT_THREAD, login) is True
    assert fills.may_fill(OTHER_THREAD, login) is False  # another thread
    assert fills.may_fill(AGENT_THREAD, world["other_login"]) is False  # another login
    assert fills.may_fill("worker-thread", login) is False  # not even its own subagent
    clock = fills.time.monotonic
    monkeypatch.setattr(fills.time, "monotonic", lambda: clock() + 61)
    assert fills.may_fill(AGENT_THREAD, login) is False  # after expiry
    assert fills.listed(PERSON) == []


def test_an_out_of_date_login_cannot_be_requested(world):
    saved_logins.mark_out_of_date(world["login"])
    with pytest.raises(fills.FillApprovalError) as refused:
        fills.request_fill(AGENT_THREAD, world["login"])
    assert str(refused.value) == (
        "The saved login for https://example.com (me@example.com) is out of date. "
        "The person has a step to sign in to https://example.com themselves."
    )
    assert fills.listed(PERSON) == []
    # Saving it again makes it usable again.
    saved_logins.save_login(
        site="https://example.com", username="me@example.com", password="[REDACTED]", saved_by=PERSON,
    )
    assert fills.request_fill(AGENT_THREAD, world["login"])["state"] == "awaiting_user"


def test_a_live_grant_stops_filling_once_its_login_is_out_of_date_or_gone(world):
    login, other = world["login"], world["other_login"]
    _approve(fills.request_fill(AGENT_THREAD, login))
    _approve(fills.request_fill(AGENT_THREAD, other))
    assert fills.may_fill(AGENT_THREAD, login) is True and fills.may_fill(AGENT_THREAD, other) is True
    saved_logins.mark_out_of_date(login)
    assert fills.may_fill(AGENT_THREAD, login) is False
    saved_logins.delete_login(other)
    assert fills.may_fill(AGENT_THREAD, other) is False


def test_deny_and_revoke_end_it(world):
    login = world["login"]
    row = fills.request_fill(AGENT_THREAD, login)
    denied = fills.deny_fill(row["id"], user_id=PERSON, revision=row["revision"],
                             application_hash=row["application_hash"])
    assert denied["state"] == "denied"
    assert fills.may_fill(AGENT_THREAD, login) is False
    with pytest.raises(fills.FillApprovalNotFound, match="^This sign-in request is closed. Ask the agent to try "
                       "again if it still needs to sign in.$"):
        _approve(row)  # a denied request can't be approved afterwards
    granted = _approve(fills.request_fill(AGENT_THREAD, login))
    assert fills.may_fill(AGENT_THREAD, login) is True
    revoked = fills.revoke_fill(granted["id"], user_id=PERSON, revision=granted["revision"],
                                application_hash=granted["application_hash"])
    assert revoked["state"] == "revoked"
    assert fills.may_fill(AGENT_THREAD, login) is False
    with pytest.raises(fills.FillApprovalNotFound, match="^This approval has ended.$"):
        fills.revoke_fill(granted["id"], user_id=PERSON, revision=granted["revision"],
                          application_hash=granted["application_hash"])


def test_decisions_need_the_current_request_of_the_same_person(world):
    row = fills.request_fill(AGENT_THREAD, world["login"])
    with pytest.raises(fills.FillApprovalNotFound):
        _approve(row, user_id=OTHER_PERSON)
    with pytest.raises(fills.FillApprovalError, match="^This request has new details. Review them, then approve or reject it.$"):
        fills.approve_fill(row["id"], user_id=PERSON, revision=row["revision"] + 1,
                           application_hash=row["application_hash"], duration_seconds=60)
    with pytest.raises(fills.FillApprovalError):
        fills.deny_fill(row["id"], user_id=PERSON, revision=row["revision"] + 1,
                        application_hash=row["application_hash"])  # a reject on an old view is refused too
    with pytest.raises(fills.FillApprovalNotFound):
        fills.revoke_fill(row["id"], user_id=PERSON, revision=row["revision"],
                          application_hash=row["application_hash"])  # not granted yet
    assert fills.may_fill(AGENT_THREAD, world["login"]) is False


@pytest.mark.parametrize("seconds", [0, -1, 1801, 3600, True, 60.0, "60", None])
def test_a_duration_outside_one_to_1800_seconds_is_refused_not_clamped(world, seconds):
    row = fills.request_fill(AGENT_THREAD, world["login"])
    with pytest.raises(fills.FillDurationError):
        _approve(row, seconds=seconds)
    assert fills.may_fill(AGENT_THREAD, world["login"]) is False
    assert fills.listed(PERSON)[0]["state"] == "awaiting_user"


def test_the_longest_duration_is_the_host_access_cap(world):
    granted = _approve(fills.request_fill(AGENT_THREAD, world["login"]), seconds=1800)
    assert granted["duration_seconds"] == fills.MAX_GRANT_SECONDS == 1800


#: The drawer's shortest duration choice (FILL_DURATIONS_SECONDS in frontend leaseFormat.ts).
SHORTEST_DRAWER_CHOICE = 300


def test_every_request_offers_at_least_the_drawers_shortest_choice(world):
    """The drawer shows Approve only with a duration to pick, so the server never offers less than its shortest one."""
    assert fills.MAX_GRANT_SECONDS >= SHORTEST_DRAWER_CHOICE
    rows = [fills.request_fill(AGENT_THREAD, world["login"]), fills.request_fill(OTHER_THREAD, world["other_login"])]
    assert all(row["max_duration_seconds"] >= SHORTEST_DRAWER_CHOICE for row in rows)
    assert all(row["max_duration_seconds"] >= SHORTEST_DRAWER_CHOICE for row in fills.listed(PERSON))
    _approve(rows[0], seconds=SHORTEST_DRAWER_CHOICE)
    assert fills.may_fill(AGENT_THREAD, world["login"]) is True


def test_a_call_naming_another_thread_from_inside_an_agents_turn_is_refused(world):
    """C3: the running turn's own thread is the only one a call may name."""
    from infra import state

    login = world["login"]
    granted = _approve(fills.request_fill(OTHER_THREAD, login))
    token = state._current_thread_id.set(AGENT_THREAD)
    try:
        with pytest.raises(fills.FillApprovalError, match="own thread"):
            fills.request_fill(OTHER_THREAD, login)
        assert fills.may_fill(OTHER_THREAD, login) is False  # granted, but not to this turn's thread
        own = fills.request_fill(AGENT_THREAD, login)
        assert own["thread_id"] == AGENT_THREAD
    finally:
        state._current_thread_id.reset(token)
    assert fills.may_fill(OTHER_THREAD, login) is True
    assert granted["thread_id"] == OTHER_THREAD


def test_auto_approve_never_approves_a_fill_and_no_lease_is_made(world):
    from infra.settings import save_setting_merge

    save_setting_merge("agents", {"defaults": {"host_access_auto_approve": True}})
    row = fills.request_fill(AGENT_THREAD, world["login"])
    assert row["state"] == "awaiting_user"
    assert fills.may_fill(AGENT_THREAD, world["login"]) is False
    _approve(row)
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM host_access_leases").fetchone()[0] == 0
    finally:
        db.close()


def test_a_restart_drops_every_grant(world):
    _approve(fills.request_fill(AGENT_THREAD, world["login"]))
    assert fills.may_fill(AGENT_THREAD, world["login"]) is True
    restarted = importlib.reload(fills)
    assert restarted.may_fill(AGENT_THREAD, world["login"]) is False
    assert restarted.listed(PERSON) == []


def test_no_agent_tool_names_the_fill_approval_module():
    """Only the person's routes, the fill executor and its result recorder reach these functions; no tool does."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    callers = {
        path.relative_to(backend).as_posix()
        for path in backend.rglob("*.py")
        if ".venv" not in path.parts and "tests" not in path.parts
        and "saved_login_fills" in path.read_text(encoding="utf-8", errors="replace")
    }
    # The person's approval routes, the person's login delete route, and the rejected-fill recorder.
    assert callers == {"api/host_access.py", "api/saved_logins.py", "infra/saved_login_results.py"}


# --- The person's routes -------------------------------------------------------------------------


def _client():
    from api.host_access import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _listed(client, credential):
    response = client.get(f"/api/host-access/leases?root_thread_id={AGENT_THREAD}", headers={
        "Authorization": f"Bearer {credential}", INTERACTIVE_UI_HEADER: "1",
    })
    assert response.status_code == 200
    return next(row for row in response.json()["leases"] if row["kind"] == "fill")


def _post(client, credential, row, verb, capability, **extra):
    return client.post(f"/api/host-access/leases/fills/{row['id']}/{verb}", json={
        "revision": row["revision"], "application_hash": row["application_hash"], **extra,
    }, headers={"Authorization": f"Bearer {credential}", USER_DECISION_HEADER: capability})


@pytest.mark.parametrize("kind", ["api", "service"])
def test_an_agent_token_cannot_approve_deny_or_revoke(world, kind):
    from infra.accounts import issue_token

    session, _ = issue_token(PERSON, kind="session")
    machine, _ = issue_token(PERSON, kind=kind)
    fills.request_fill(AGENT_THREAD, world["login"])
    with _client() as client:
        pending = _listed(client, session)
        capability = pending["decision_capability"]
        machine_view = client.get("/api/host-access/leases", headers={"Authorization": f"Bearer {machine}"})
        assert all("decision_capability" not in row for row in machine_view.json()["leases"])
        assert client.get("/api/host-access/leases", headers={
            "Authorization": f"Bearer {machine}", INTERACTIVE_UI_HEADER: "1",
        }).status_code == 403
        assert _post(client, machine, pending, "approve", capability, duration_seconds=600).status_code == 403
        assert _post(client, machine, pending, "reject", capability).status_code == 403
        assert fills.may_fill(AGENT_THREAD, world["login"]) is False
        granted = _post(client, session, pending, "approve", capability, duration_seconds=600)
        assert granted.status_code == 200 and granted.json()["state"] == "active"
        active = _listed(client, session)
        assert _post(client, machine, active, "revoke", active["decision_capability"]).status_code == 403
        assert fills.may_fill(AGENT_THREAD, world["login"]) is True


def test_a_persons_session_approves_denies_and_revokes(world):
    from infra.accounts import issue_token

    session, _ = issue_token(PERSON, kind="session")
    fills.request_fill(AGENT_THREAD, world["login"])
    with _client() as client:
        pending = _listed(client, session)
        assert pending["agent"] == "Bookkeeper" and pending["site"] == "https://example.com"
        assert pending["created_at"].endswith("Z")
        assert _post(client, session, pending, "approve", "", duration_seconds=60).status_code == 403
        assert _post(client, session, pending, "approve", pending["decision_capability"],
                     duration_seconds=1801).status_code == 400
        approved = _post(client, session, pending, "approve", pending["decision_capability"], duration_seconds=300)
        assert approved.status_code == 200
        assert approved.json()["expires_at"].endswith("Z") and approved.json()["duration_seconds"] == 300
        assert fills.may_fill(AGENT_THREAD, world["login"]) is True
        assert _post(client, session, pending, "approve", pending["decision_capability"],
                     duration_seconds=300).status_code == 404  # already decided
        active = _listed(client, session)
        assert active["state"] == "active"
        revoked = _post(client, session, active, "revoke", active["decision_capability"])
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
        assert fills.may_fill(AGENT_THREAD, world["login"]) is False

        fills.request_fill(AGENT_THREAD, world["login"])
        again = _listed(client, session)
        denied = _post(client, session, again, "reject", again["decision_capability"])
        assert denied.status_code == 200 and denied.json()["state"] == "denied"
        assert fills.may_fill(AGENT_THREAD, world["login"]) is False


def test_another_persons_session_cannot_see_or_decide(world):
    from infra.accounts import issue_token

    mine, _ = issue_token(PERSON, kind="session")
    theirs, _ = issue_token(OTHER_PERSON, kind="session")
    fills.request_fill(AGENT_THREAD, world["login"])
    with _client() as client:
        pending = _listed(client, mine)
        seen = client.get("/api/host-access/leases", headers={
            "Authorization": f"Bearer {theirs}", INTERACTIVE_UI_HEADER: "1",
        }).json()["leases"]
        assert [row for row in seen if row.get("kind") == "fill"] == []
        assert _post(client, theirs, pending, "approve", pending["decision_capability"],
                     duration_seconds=60).status_code == 404
    assert fills.may_fill(AGENT_THREAD, world["login"]) is False


# --- Product events --------------------------------------------------------------------------------


def test_each_fill_transition_counts_once_with_only_the_thread_and_minutes(world, monkeypatch):
    """One `saved_login` event per transition; never the site, username, domain or any id but the thread."""
    from infra import product_events
    from infra.accounts import issue_token

    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    session, _ = issue_token(PERSON, kind="session")

    first = fills.request_fill(AGENT_THREAD, world["login"])
    fills.request_fill(AGENT_THREAD, world["login"])  # the same request again: no second event
    with _client() as client:
        pending = _listed(client, session)
        assert _post(client, session, pending, "approve", pending["decision_capability"],
                     duration_seconds=900).status_code == 200
        active = _listed(client, session)
        assert _post(client, session, active, "revoke", active["decision_capability"]).status_code == 200
        second = fills.request_fill(AGENT_THREAD, world["login"])
        again = _listed(client, session)
        assert _post(client, session, again, "reject", again["decision_capability"]).status_code == 200
        assert _post(client, session, again, "reject", again["decision_capability"]).status_code == 404
        third = fills.request_fill(AGENT_THREAD, world["login"])
        fresh = _listed(client, session)
        assert _post(client, session, fresh, "approve", fresh["decision_capability"],
                     duration_seconds=60).status_code == 200
        clock = fills.time.monotonic
        monkeypatch.setattr(fills.time, "monotonic", lambda: clock() + 61)
        client.get("/api/host-access/leases", headers={"Authorization": f"Bearer {session}"})  # the read that drops it
        fills.listed(PERSON)  # already dropped: counted once

    assert [(args, sorted(kwargs)) for args, kwargs in events] == [
        (("saved_login", "fill_requested"), ["properties", "thread_id"]),
        (("saved_login", "fill_approved"), ["properties", "thread_id"]),
        (("saved_login", "fill_revoked"), ["properties", "thread_id"]),
        (("saved_login", "fill_requested"), ["properties", "thread_id"]),
        (("saved_login", "fill_denied"), ["properties", "thread_id"]),
        (("saved_login", "fill_requested"), ["properties", "thread_id"]),
        (("saved_login", "fill_approved"), ["properties", "thread_id"]),
        (("saved_login", "fill_expired"), ["properties", "thread_id"]),
    ]
    assert all(kwargs["thread_id"] == AGENT_THREAD for _, kwargs in events)
    approvals = [kwargs["properties"] for args, kwargs in events if args[1] == "fill_approved"]
    assert approvals == [{"duration_minutes": 15}, {"duration_minutes": 1}]
    assert all(kwargs["properties"] is None for args, kwargs in events if args[1] != "fill_approved")
    payloads = repr(events)
    for private in ("example.com", "example.org", "example", "me@example.com", world["login"], world["other_login"],
                    first["id"], second["id"], third["id"]):
        assert private not in payloads


# --- The duration an agent asks for, and the person's "Until I turn it off" ----------------------

#: A plain fake value for saving a login again; never a real password.
RESAVED = "resaved-fake-value"


def _approve_standing(row, user_id=PERSON):
    return fills.approve_fill(
        row["id"], user_id=user_id, revision=row["revision"], application_hash=row["application_hash"],
        until_revoked=True,
    )


def test_an_agent_may_ask_for_the_time_its_task_needs_and_it_is_kept_on_the_request(world):
    row = fills.request_fill(AGENT_THREAD, world["login"], requested_seconds=900)
    assert row["requested_duration_seconds"] == 900 and row["standing"] is False
    assert fills.listed(PERSON)[0]["requested_duration_seconds"] == 900
    # A request already waiting is returned as it is, whatever is asked the second time.
    again = fills.request_fill(AGENT_THREAD, world["login"], requested_seconds=60)
    assert again["id"] == row["id"] and again["requested_duration_seconds"] == 900
    assert fills.request_fill(OTHER_THREAD, world["login"])["requested_duration_seconds"] is None
    longest = fills.request_fill(AGENT_THREAD, world["other_login"], requested_seconds=fills.MAX_GRANT_SECONDS)
    assert longest["requested_duration_seconds"] == 1800
    # The person picks; the request's duration is only where the drawer starts.
    assert _approve(row, seconds=300)["duration_seconds"] == 300
    # The duration is keyword-only, so no positional slip can set it.
    with pytest.raises(TypeError):
        fills.request_fill(OTHER_THREAD, world["other_login"], 900)  # type: ignore[misc]


@pytest.mark.parametrize("requested", [
    1801, 3600, 0, -1, 60.0, 900.5, "900", True, False, "indefinite", "until_revoked", "standing",
    float("inf"), {"until_revoked": True}, [900],
])
def test_an_agent_cannot_ask_for_a_standing_or_out_of_range_duration(world, requested):
    with pytest.raises(fills.FillDurationError):
        fills.request_fill(AGENT_THREAD, world["login"], requested_seconds=requested)
    assert fills.listed(PERSON) == []
    assert fills._approvals == {}
    assert world["nudges"] == []


def test_until_revoked_stays_live_past_the_cap_until_revoked(world, monkeypatch):
    login = world["login"]
    row = fills.request_fill(AGENT_THREAD, login, requested_seconds=900)
    granted = _approve_standing(row)
    assert granted["state"] == "active" and granted["standing"] is True
    assert granted["duration_seconds"] is None and granted["expires_at"] is None
    assert fills.may_fill(AGENT_THREAD, login) is True
    clock = fills.time.monotonic
    monkeypatch.setattr(fills.time, "monotonic", lambda: clock() + fills.MAX_GRANT_SECONDS * 100)
    assert fills.may_fill(AGENT_THREAD, login) is True
    [listed] = fills.listed(PERSON)
    assert listed["id"] == granted["id"] and listed["state"] == "active" and listed["standing"] is True
    assert fills.may_fill(OTHER_THREAD, login) is False  # another thread
    assert fills.may_fill(AGENT_THREAD, world["other_login"]) is False  # another login
    assert fills.may_fill("worker-thread", login) is False  # not even its own subagent
    revoked = fills.revoke_fill(granted["id"], user_id=PERSON, revision=granted["revision"],
                                application_hash=granted["application_hash"])
    assert revoked["state"] == "revoked"
    assert fills.may_fill(AGENT_THREAD, login) is False
    assert fills.listed(PERSON) == []


def test_until_revoked_ends_at_a_restart(world):
    _approve_standing(fills.request_fill(AGENT_THREAD, world["login"]))
    assert fills.may_fill(AGENT_THREAD, world["login"]) is True
    restarted = importlib.reload(fills)
    assert restarted.may_fill(AGENT_THREAD, world["login"]) is False
    assert restarted.listed(PERSON) == []


def test_until_revoked_ends_when_its_login_goes_out_of_date_and_a_resave_does_not_revive_it(world):
    login = world["login"]
    _approve_standing(fills.request_fill(AGENT_THREAD, login))
    _approve_standing(fills.request_fill(AGENT_THREAD, world["other_login"]))
    saved_logins.mark_out_of_date(login)  # by no path that ends its grants
    # The check at use fails closed and ends it; no read sweeps for it.
    assert fills.may_fill(AGENT_THREAD, login) is False
    assert [row["site"] for row in fills.listed(PERSON)] == ["https://example.org"]
    saved_logins.save_login(site="https://example.com", username="me@example.com", password=RESAVED, saved_by=PERSON)
    assert fills.may_fill(AGENT_THREAD, login) is False
    assert fills.may_fill(AGENT_THREAD, world["other_login"]) is True


def test_reading_the_list_or_one_request_does_not_look_up_any_login(world, monkeypatch):
    _approve_standing(fills.request_fill(AGENT_THREAD, world["login"]))
    _approve(fills.request_fill(AGENT_THREAD, world["other_login"]))
    looked_up: list[object] = []
    monkeypatch.setattr(saved_logins, "get_login", lambda *args, **kwargs: looked_up.append(args))
    rows = fills.listed(PERSON)
    fills.get(rows[0]["id"], user_id=PERSON)
    assert len(rows) == 2 and looked_up == []


def test_a_login_going_out_of_date_ends_its_waiting_requests_too(world, monkeypatch):
    from infra import product_events, saved_login_results

    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    login = world["login"]
    _approve_standing(fills.request_fill(AGENT_THREAD, login))
    waiting = fills.request_fill(OTHER_THREAD, login, requested_seconds=900)
    kept = fills.request_fill(OTHER_THREAD, world["other_login"])
    events.clear()
    world["nudges"].clear()
    saved_login_results.fill_failed(AGENT_THREAD, login)
    assert [row["id"] for row in fills.listed(PERSON)] == [kept["id"]]
    assert [args for args, _ in events] == [("saved_login", "fill_expired")] * 2
    assert {kwargs["thread_id"] for _, kwargs in events} == {AGENT_THREAD, OTHER_THREAD}
    assert len(world["nudges"]) == 2
    # The person's approve gets the closed-request answer an ended request already gives, for either choice.
    for choice in ({"duration_seconds": 900}, {"until_revoked": True}):
        with pytest.raises(fills.FillApprovalNotFound, match="^This sign-in request is closed. Ask the agent to try "
                           "again if it still needs to sign in.$"):
            fills.approve_fill(waiting["id"], user_id=PERSON, revision=waiting["revision"],
                               application_hash=waiting["application_hash"], **choice)
    assert fills.may_fill(OTHER_THREAD, login) is False
    # The waiting agent's next ask gets the out-of-date answer it already gets, with the person's step.
    with pytest.raises(fills.FillApprovalError) as refused:
        fills.request_fill(OTHER_THREAD, login)
    assert str(refused.value) == (
        "The saved login for https://example.com (me@example.com) is out of date. "
        "The person has a step to sign in to https://example.com themselves."
    )


def test_a_rejected_fill_ends_the_grant_before_the_login_can_be_saved_again(world):
    from infra import saved_login_results

    login = world["login"]
    _approve_standing(fills.request_fill(AGENT_THREAD, login))
    saved_login_results.fill_failed(AGENT_THREAD, login)
    assert fills._approvals == {}  # ended at once, with no read in between
    saved_logins.save_login(site="https://example.com", username="me@example.com", password=RESAVED, saved_by=PERSON)
    assert fills.may_fill(AGENT_THREAD, login) is False


@pytest.mark.parametrize("extra", [
    {"duration_seconds": 600, "until_revoked": True},
    {"duration_seconds": None, "until_revoked": False},
    {"until_revoked": 1},
    {"until_revoked": "yes"},
])
def test_approve_needs_exactly_one_of_a_duration_or_until_revoked(world, extra):
    row = fills.request_fill(AGENT_THREAD, world["login"])
    with pytest.raises(fills.FillDurationError):
        fills.approve_fill(row["id"], user_id=PERSON, revision=row["revision"],
                           application_hash=row["application_hash"], **extra)
    assert fills.listed(PERSON)[0]["state"] == "awaiting_user"
    assert fills.may_fill(AGENT_THREAD, world["login"]) is False


def _routes_client():
    from api.host_access import router
    from api.saved_logins import router as saved_logins_router

    app = FastAPI()
    app.include_router(router)
    app.include_router(saved_logins_router)
    return TestClient(app)


@pytest.mark.parametrize("kind", ["api", "service"])
def test_an_agent_token_cannot_approve_until_revoked(world, kind):
    from infra.accounts import issue_token

    session, _ = issue_token(PERSON, kind="session")
    machine, _ = issue_token(PERSON, kind=kind)
    fills.request_fill(AGENT_THREAD, world["login"], requested_seconds=900)
    with _client() as client:
        pending = _listed(client, session)
        response = _post(client, machine, pending, "approve", pending["decision_capability"], until_revoked=True)
        assert response.status_code == 403
        assert fills.may_fill(AGENT_THREAD, world["login"]) is False
        assert _listed(client, session)["state"] == "awaiting_user"


def test_the_route_refuses_both_or_neither_and_approves_until_revoked(world):
    from infra.accounts import issue_token

    session, _ = issue_token(PERSON, kind="session")
    fills.request_fill(AGENT_THREAD, world["login"], requested_seconds=900)
    with _client() as client:
        pending = _listed(client, session)
        assert pending["requested_duration_seconds"] == 900 and pending["standing"] is False
        capability = pending["decision_capability"]
        for body in ({"duration_seconds": 600, "until_revoked": True}, {}, {"until_revoked": False},
                     {"until_revoked": "true"}, {"until_revoked": 1}, {"duration_seconds": None}):
            response = _post(client, session, pending, "approve", capability, **body)
            assert 400 <= response.status_code < 500, body
        for body in ({"duration_seconds": 600, "until_revoked": True}, {}):  # both, and neither
            response = _post(client, session, pending, "approve", capability, **body)
            assert response.status_code == 422, body
            assert "Pick one: a duration or until revoked." in response.text, body
        assert fills.may_fill(AGENT_THREAD, world["login"]) is False
        granted = _post(client, session, pending, "approve", capability, until_revoked=True)
        assert granted.status_code == 200
        assert granted.json()["standing"] is True and granted.json()["expires_at"] is None
        assert fills.may_fill(AGENT_THREAD, world["login"]) is True
        active = _listed(client, session)
        assert active["state"] == "active" and active["standing"] is True
        assert _post(client, session, active, "revoke", active["decision_capability"]).status_code == 200
        assert fills.may_fill(AGENT_THREAD, world["login"]) is False


def test_deleting_a_login_ends_every_request_and_grant_for_it(world, monkeypatch):
    from infra import product_events
    from infra.accounts import issue_token

    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    session, _ = issue_token(PERSON, kind="session")
    login, other = world["login"], world["other_login"]
    _approve_standing(fills.request_fill(AGENT_THREAD, login))
    pending = fills.request_fill(OTHER_THREAD, login)
    kept = _approve(fills.request_fill(AGENT_THREAD, other))
    events.clear()
    with _routes_client() as client:
        response = client.delete(f"/api/saved-logins/{login}", headers={"Authorization": f"Bearer {session}"})
        assert response.status_code == 200
    assert fills.may_fill(AGENT_THREAD, login) is False
    assert [row["id"] for row in fills.listed(PERSON)] == [kept["id"]]
    assert pending["id"] not in fills._approvals
    assert [args for args, _ in events] == [("saved_login", "fill_revoked")] * 2
    assert {kwargs["thread_id"] for _, kwargs in events} == {AGENT_THREAD, OTHER_THREAD}
    assert all(kwargs["properties"] is None for _, kwargs in events)
    assert fills.may_fill(AGENT_THREAD, other) is True


def test_duration_events_carry_only_the_thread_and_minutes_or_standing(world, monkeypatch):
    from infra import product_events

    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    timed = fills.request_fill(AGENT_THREAD, world["login"], requested_seconds=900)
    standing = fills.request_fill(OTHER_THREAD, world["other_login"])
    _approve(timed, seconds=1800)
    _approve_standing(standing)
    assert [(args[1], kwargs["properties"]) for args, kwargs in events] == [
        ("fill_requested", {"requested_minutes": 15}),
        ("fill_requested", None),
        ("fill_approved", {"duration_minutes": 30}),
        ("fill_approved", {"standing": True}),
    ]
    assert all(sorted(kwargs) == ["properties", "thread_id"] for _, kwargs in events)
    payloads = repr(events)
    for private in ("example.com", "example.org", "example", "me@example.com", world["login"], world["other_login"],
                    timed["id"], standing["id"], timed["application_hash"]):
        assert private not in payloads
