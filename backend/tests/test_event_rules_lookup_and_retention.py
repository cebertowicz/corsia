"""Event rules: a failed repository lookup, decision rows outliving their rule or
thread, how long a disabled rule is listed, repository case, malformed facts, the
preparation gate, and reads per recipient thread."""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from events.connectors import library
from events.platform import deliver_platform_event_sync
from tests.event_routing_helpers import register_pr, route
from tests.test_event_rules_2a import (  # noqa: F401 (fixtures)
    MERGED, P, REPO, _agent, _decisions, _library_changed, _library_owner, _merged, _no_analytics,
    _project, _rows, _statements, _tool, _wakes, _write, resolved, sent,
)

FEED = "events.connectors.github_notifications_feed"


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    yield seen
    logger.remove(sink)


async def _self_rule(thread_id: str, repo: str = REPO) -> dict:
    return await _tool(thread_id, operation="create", connector="github", event_type="pr.merged",
                       match=[{"field": "repo", "op": "eq", "value": repo}], note="PR merged.")


# -- 1. A failed repository lookup fails closed, and says so -----------------------------------

@pytest.mark.asyncio
async def test_a_failed_repository_lookup_fires_no_self_rule_and_records_why(
        isolated_db, sent, monkeypatch, warnings):
    from events.connectors.github_notifications_feed import FeedError

    _project(P)
    _agent("a")
    rule = await _self_rule("a")

    def _unreadable(_repository):
        raise FeedError("repo_scope_unavailable")

    monkeypatch.setattr(f"{FEED}.projects_tracking_github_repository", _unreadable)
    await route(MERGED, _merged(42))

    assert _wakes("a") == [] and sent == []
    [decision] = _decisions(rule_id=rule["id"])
    assert (decision["thread_id"], decision["outcome"], decision["reason"]) == (
        "a", "dead_lettered", "repository lookup failed")
    assert any("repository lookup failed for github pr.merged on o/r" in line for line in warnings)
    [why] = (await _tool("a", operation="explain", event_id=decision["event_id"]))["decisions"]
    assert why["explanation"] == f"rule {rule['id']} could not deliver it: repository lookup failed"


# -- 2. No decision row outlives its rule or thread --------------------------------------------

def _decision_row(rule_id: str, thread_id: str | None) -> None:
    _write("INSERT INTO event_route_decisions (at, event_id, connector, event_type, watch_event, "
           "rule_id, thread_id, outcome, via) VALUES ('2026-10-21T00:00:00+00:00', 'e', 'github', "
           "'pr.merged', ?, ?, ?, 'woke', 'rule')", MERGED, rule_id, thread_id)


@pytest.mark.asyncio
async def test_deleting_a_thread_deletes_the_decisions_naming_it_or_its_rules(isolated_db, monkeypatch):
    from api import thread_mutations

    async def _quiesce(_thread_id, *, user_id):
        return {}

    monkeypatch.setattr(thread_mutations, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(thread_mutations.runtime_boundary, "quiesce_thread_runtimes", _quiesce)
    _project(P)
    _agent("writer")
    _agent("other")
    written = await _self_rule("writer")
    kept = await _self_rule("other")
    _decision_row(written["id"], "other")   # its rule goes
    _decision_row(kept["id"], "writer")     # it names the thread
    _decision_row(kept["id"], "other")      # neither: it stays

    await thread_mutations.delete_thread("writer", "token")

    assert [(row["rule_id"], row["thread_id"]) for row in _decisions()] == [(kept["id"], "other")]


@pytest.mark.asyncio
async def test_pruning_a_disabled_rule_deletes_its_decisions(isolated_db):
    _project(P)
    _agent("writer")
    stale = await _self_rule("writer")
    live = await _self_rule("writer")
    _decision_row(stale["id"], "writer")
    _decision_row(live["id"], "writer")
    await _tool("writer", operation="disable", rule_id=stale["id"])
    _write("UPDATE event_rules SET updated_at=? WHERE id=?", "2026-08-01T00:00:00+00:00", stale["id"])

    await _self_rule("writer")  # the next write prunes it

    assert stale["id"] not in {row["id"] for row in _rows("SELECT id FROM event_rules")}
    assert [row["rule_id"] for row in _decisions()] == [live["id"]]


@pytest.mark.asyncio
async def test_a_disabled_rule_leaves_the_list_after_retention_with_nothing_else_written(isolated_db):
    _project(P)
    _agent("writer")
    stale = await _self_rule("writer")
    recent = await _self_rule("writer")
    live = await _self_rule("writer")
    for rule in (stale, recent):
        await _tool("writer", operation="disable", rule_id=rule["id"])
    _write("UPDATE event_rules SET updated_at=? WHERE id=?", "2026-08-01T00:00:00+00:00", stale["id"])

    listed = (await _tool("writer", operation="list"))["rules"]

    assert {rule["id"] for rule in listed} == {recent["id"], live["id"]}


# -- 3. Repository facts match without case, as create validates them -------------------------

@pytest.mark.asyncio
async def test_a_rule_on_a_repository_matches_its_webhooks_in_any_case(
        isolated_db, sent, monkeypatch):
    real = __import__(FEED, fromlist=["_"]).projects_tracking_github_repository
    monkeypatch.setattr(f"{FEED}.projects_tracking_github_repository",
                        lambda repository: real(repository.lower()))
    _project(P)
    _agent("owner")
    _agent("watcher")
    routed = await _tool("watcher", operation="create", connector="github", event_type="pr.merged",
                         match=[{"field": "repo", "op": "in", "value": ["x/y", REPO]}])
    unrouted = await _tool("owner", operation="create", connector="github", event_type="pr.merged",
                           match=[{"field": "repo", "op": "prefix", "value": "o/"},
                                  {"field": "repo", "op": "eq", "value": REPO}])
    register_pr("O/R", 88, "owner")

    await route(MERGED, _merged(88, repo="O/R"))  # routed to its owner
    await route(MERGED, _merged(89, repo="O/R"))  # routing gives it no thread

    # Both match; the hourly cap digests the second wake.
    assert [row["outcome"] for row in _decisions(rule_id=routed["id"])] == ["woke", "digested"]
    [decision] = _decisions(rule_id=unrouted["id"], via="rule")
    assert (decision["thread_id"], decision["outcome"]) == ("owner", "woke")


# -- 4. Malformed identity facts are logged --------------------------------------------------

def test_a_webhook_missing_an_identity_fact_is_logged(isolated_db, warnings):
    from events.connectors.github_route_preparation import PreparedRoute
    from events.connectors.github_rules import apply_routed_rules

    payload = _merged(42)
    del payload["repository"]

    apply_routed_rules(PreparedRoute(), MERGED, payload, None, {})

    [line] = [line for line in warnings if "event rules" in line]
    assert MERGED in line and "missing identity fact(s) ['repo']" in line


# -- 5. The repository lookup never holds the preparation gate --------------------------------

@pytest.mark.asyncio
async def test_the_repository_lookup_runs_outside_the_preparation_gate(isolated_db, sent, monkeypatch):
    from events.connectors.github_route_preparation import _preparation_gate

    gate = _preparation_gate()
    held: list[bool] = []
    monkeypatch.setattr(f"{FEED}.projects_tracking_github_repository",
                        lambda _repository: held.append(gate.locked()) or frozenset({P}))
    _project(P)
    _agent("a")
    await _self_rule("a")

    await route(MERGED, _merged(42))

    assert held == [False]
    assert len(_wakes("a")) == 1


# -- 6. Pause and the wake budget are read once per thread -------------------------------------

def _reads_per_thread(monkeypatch, rule_count: int, manifest: str) -> tuple[int, int]:
    """The (paused, wake budget) reads one event makes with ``rule_count`` wake rules
    of one thread."""
    paused: list[str] = []

    def _paused(_db, thread):
        paused.append(thread)

    monkeypatch.setattr("plans.public.thread_paused_since", _paused)
    for _ in range(rule_count):
        asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                          event_type=library.ITEM_CHANGED_EVENT))
    with _statements() as seen:
        deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed(manifest))
    budget = [s for s in seen if s.lstrip().upper().startswith("SELECT")
              and "FROM event_route_decisions" in s]
    _write("DELETE FROM event_rules")
    _write("DELETE FROM event_route_decisions")
    return len(paused), len(budget)


def test_two_wake_rules_for_one_thread_read_pause_and_budget_once(isolated_db, monkeypatch):
    _project(P)
    _agent("owner")
    _library_owner("owner")

    one = _reads_per_thread(monkeypatch, 1, "m-1")
    two = _reads_per_thread(monkeypatch, 2, "m-2")

    assert one == two == (1, 1)
