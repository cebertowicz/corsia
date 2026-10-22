"""Event rules only add: a recipient refusing the delivery or rule evaluation failing
never costs routing its delivery; a rule whose writer left its project turns itself off."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from events.connectors import library
from events.platform import deliver_platform_event_sync
from events.router import RouteOutcome
from infra.admission_fence import fence
from tests.event_routing_helpers import register_pr, route
from tests.test_event_rules_2a import (  # noqa: F401 (fixtures)
    P, Q, REPO, SYNC, _agent, _decisions, _library_changed, _library_owner, _no_analytics,
    _notes, _project, _rows, _sync, _tool, _wakes, _write, resolved, sent,
)

UNAVAILABLE = ("dead_lettered", "recipient unavailable")


async def _watch_pushes(thread_id: str, **kwargs) -> dict:
    return await _tool(thread_id, operation="create", connector="github",
                       event_type="pr.synchronize",
                       match=[{"field": "repo", "op": "eq", "value": REPO}], **kwargs)


def _outcomes(rule_id: str) -> list[tuple[str, str | None]]:
    return [(row["outcome"], row["reason"]) for row in _decisions(rule_id=rule_id)]


# -- A recipient a rule adds refusing the delivery costs routing nothing ----------------------

@pytest.mark.asyncio
async def test_a_refused_rule_recipient_never_costs_the_routed_owner_its_webhook(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    rule = await _watch_pushes("watcher", note="Re-run the load check.")

    with fence() as held:
        held.add(["watcher"])  # a delete in progress holds it; its row still reads available
        result = await route(SYNC, _sync(7, 1))

    assert result.outcome == RouteOutcome.ACCEPTED
    [(thread, content)] = sent
    # The routed owner got its delivery, and with it the notice its preparation claimed.
    assert thread == "owner" and "[CORSIA PR notice] You own PR o/r#7" in content
    assert _rows("SELECT key FROM settings WHERE key LIKE '%o/r#7'")
    assert _wakes("watcher") == [] and _notes("watcher") == []
    assert _outcomes(rule["id"]) == [UNAVAILABLE]


def _locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["apply_webhook_rules_sync", "rule_reads.type_rules"])
async def test_failing_rule_evaluation_never_costs_the_routed_owner_its_webhook(
        isolated_db, sent, monkeypatch, failing):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    await _watch_pushes("watcher", note="Re-run the load check.")
    monkeypatch.setattr(f"events.connectors.github_rules.{failing}", _locked)

    result = await route(SYNC, _sync(7, 1))

    assert result.outcome == RouteOutcome.ACCEPTED
    [(thread, content)] = sent
    assert thread == "owner" and "[CORSIA PR notice] You own PR o/r#7" in content
    assert _rows("SELECT key FROM settings WHERE key LIKE '%o/r#7'")
    assert _wakes("watcher") == []
    # The failure is only logged: no outcome row, since none of them is a rule's.
    assert _rows("SELECT 1 FROM event_route_decisions") == []


def test_a_refused_rule_recipient_never_rolls_back_the_platform_delivery(isolated_db):
    _project(P)
    _agent("owner")
    _agent("watcher")
    _library_owner("owner")
    rule = asyncio.run(_tool("watcher", operation="create", connector=library.LIBRARY_CONNECTOR,
                             event_type=library.ITEM_CHANGED_EVENT))

    with fence() as held:
        held.add(["watcher"])
        deliveries = deliver_platform_event_sync(library.LIBRARY_CONNECTOR,
                                                 _library_changed("m-1"))

    by_thread = {made.thread_id: (made.outcome, made.reason) for made in deliveries}
    assert by_thread["watcher"] == UNAVAILABLE
    assert by_thread["owner"][0] in ("woke", "digested")
    # Committed: the owner's delivery survived the refusal in the same transaction.
    assert _wakes("owner") or _notes("owner")
    assert _wakes("watcher") == [] and _notes("watcher") == []
    assert _outcomes(rule["id"]) == [UNAVAILABLE]


# -- A writer that changes project takes its rules out of the old one -------------------------

@pytest.mark.asyncio
async def test_a_writer_leaving_the_project_turns_its_rules_off_once(isolated_db, sent):
    _project(P)
    _project(Q)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    own = await _watch_pushes("watcher", note="Re-run the load check.")
    owners = await _watch_pushes("watcher", to="registered_owner", mode="digest",
                                 note="Watcher saw a push.")

    await route(SYNC, _sync(7, 1))
    assert len(_wakes("watcher")) == 1
    assert "Watcher saw a push." in sent[0][1]

    # The move is another lane's write (set_conversation_project); only the row changes.
    _write("UPDATE threads SET project_id=? WHERE id='watcher'", Q)
    await route(SYNC, _sync(7, 2))
    await route(SYNC, _sync(7, 3))

    assert len(_wakes("watcher")) == 1 and _notes("watcher") == []
    assert [thread for thread, _ in sent] == ["owner"] * 3
    assert all("Watcher saw a push." not in content for _, content in sent[1:])
    left = ("dead_lettered", "its writer left the rule's project; rule turned off")
    assert _outcomes(own["id"]) == [("woke", None), left]
    assert _outcomes(owners["id"])[1:] == [left]
    assert {row["id"]: row["enabled"] for row in _rows("SELECT id, enabled FROM event_rules")} == {
        own["id"]: 0, owners["id"]: 0}
