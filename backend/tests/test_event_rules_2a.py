"""Event rules 2.A: agents write rules; today's routing decides first, rules add or drop.

Each test drives the real entrypoints (the ``event_rules`` tool, the webhook
router, the platform delivery path) on a disposable database. Only the final
hand-off to a live turn (``send_thread_input``) is replaced, so the router's
own wake is visible as the content it would deliver.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from events.connectors import library
from events.identity import pr_external_id
from events.platform import DIGESTED, FILTERED, deliver_platform_event_sync
from events.router import RouteOutcome
from infra.database import get_db
from registry import models
from tests.event_routing_helpers import (
    check_suite_payload, mk_session, pr_payload, register_pr, route, read_statements, statements,
)
from tools.event_rules import EventRulesTool

REPO = "o/r"
P = "proj-a"
Q = "proj-b"
SYNC = "webhook.github.pr_synchronize"
MERGED = "webhook.github.pr_merged"


@pytest.fixture(autouse=True)
def _no_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


TRACKED = {P: frozenset({REPO, "x/y"}), Q: frozenset({"q/r"})}


@pytest.fixture(autouse=True)
def resolved(monkeypatch):
    """The repositories each project's registered source repositories track.

    Yields, per lookup, whether the thread's connection was inside a transaction.
    """
    in_transaction: list[bool] = []

    def _tracked(project_id=None):
        in_transaction.append(get_db().in_transaction)
        return TRACKED.get(project_id, frozenset())

    def _tracking(repository):
        in_transaction.append(get_db().in_transaction)
        return frozenset(project for project, repos in TRACKED.items() if repository in repos)

    monkeypatch.setattr("events.connectors.github_notifications_feed.tracked_github_repositories",
                        _tracked)
    monkeypatch.setattr(
        "events.connectors.github_notifications_feed.projects_tracking_github_repository",
        _tracking)
    yield in_transaction


@pytest.fixture
def sent():
    """What the router's own delivery hands to each thread: (thread, content)."""
    calls: list[tuple[str, str]] = []

    async def _send(thread_id, _row, content, **_kwargs):
        calls.append((thread_id, content))

    with patch("events.delivery.send_thread_input", AsyncMock(side_effect=_send)):
        yield calls


def _write(sql: str, *args) -> None:
    db = get_db()
    try:
        db.execute(sql, args)
        db.commit()
    finally:
        db.close()


def _rows(sql: str, *args) -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _project(project_id: str, *, conductor: str | None = None, cloud_off: bool = False) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _write("INSERT INTO projects (id, name, conductor_thread_id, created_at, updated_at) "
           "VALUES (?,?,?,?,?)", project_id, project_id, conductor, now, now)
    if cloud_off:
        _write("INSERT INTO project_inference_policies (project_id, cloud_models_off) VALUES (?,1)",
               project_id)


def _agent(thread_id: str, project: str = P, *, local_only: bool = False,
           temporary_of: str | None = None) -> str:
    mk_session(thread_id, spawned_by=temporary_of, project_id=project)
    _write("UPDATE threads SET agent_enabled=?, local_only=? WHERE id=?",
           0 if temporary_of else 1, int(local_only), thread_id)
    return thread_id


async def _tool(thread_id: str, **kwargs):
    out = await EventRulesTool().execute(_thread_id=thread_id, **kwargs)
    return out if out.startswith(("Refused:", "Error:")) else json.loads(out)


def _wakes(thread_id: str) -> list[str]:
    """Wakes a rule added: the platform path's own wake rows."""
    return [row["content"] for row in _rows(
        "SELECT content FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%' "
        "ORDER BY created_at", thread_id)]


def _notes(thread_id: str) -> list[dict]:
    return _rows("SELECT summary, details_json FROM context_change_journal "
                 "WHERE object_kind='thread_context' AND object_id=? ORDER BY seq", thread_id)


def _digest(thread_id: str) -> list[dict]:
    """The notes the thread's next passive digest shows, after collapsing.

    Newest first, as ``passive_changes`` reads them, so a collapsed note shows its latest row.
    """
    from infra.passive_digest import build_digest

    db = get_db()
    try:
        rows = db.execute("SELECT * FROM context_change_journal WHERE object_kind='thread_context' "
                          "AND object_id=? ORDER BY seq DESC", (thread_id,)).fetchall()
    finally:
        db.close()
    return build_digest(rows, overflow=False, clock=None)[1]


def _decisions(**where) -> list[dict]:
    clause = " AND ".join(f"{key}=?" for key in where) or "1"
    return _rows(f"SELECT * FROM event_route_decisions WHERE {clause} ORDER BY id", *where.values())


def _sync(number: int, n: int, **kwargs) -> dict:
    payload = pr_payload(REPO, number, action="synchronize", **kwargs)
    payload["pull_request"]["head"]["sha"] = f"sha-{n}"
    return payload


def _merged(number: int, author: str = "alice", repo: str = REPO) -> dict:
    payload = pr_payload(repo, number, action="closed")
    payload["pull_request"]["merged"] = True
    payload["pull_request"]["user"] = {"login": author, "type": "User"}
    return payload


def _comment(number: int) -> dict:
    return {
        "action": "created", "repository": {"full_name": REPO},
        "issue": {"number": number, "pull_request": {"url": "https://api/pr"}},
        "comment": {"body": "please look", "html_url": f"https://example.test/{number}#c",
                    "user": {"login": "alice", "type": "User"}},
        "sender": {"login": "alice", "type": "User"},
    }


def _watch(thread_id: str, external_id: str, event: str) -> tuple[str, str]:
    """An armed watch of ``thread_id``'s on one artifact's named event: (node, watch)."""
    from plans import service
    from plans.boundary import PlanActor, mutate
    from registry import artifact_record

    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        node = service.add_node(plan.id, kind="task", title="Await it",
                                acceptance_criteria="it happened", db=db)
        service.transition_node(node.id, "active", db=db)
        db.commit()
        mutate(
            plan.id, "arm_watch",
            {"node_id": node.id, "condition": {"type": "event", "value": event},
             "artifact_id": str(artifact_record.identity_row_for(external_id, db=db)["id"]),
             "expected_event": event,
             "expected_revision": service._node_row(db, node.id)["revision"]},
            actor=PlanActor(thread_id, "user-1", P), provenance="test.event_rules",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
        watch = db.execute("SELECT id FROM expected_event_watches WHERE plan_node_id=?",
                           (node.id,)).fetchone()["id"]
        return node.id, watch
    finally:
        db.close()


def _armed(node_id: str) -> int:
    return len(_rows("SELECT 1 FROM expected_event_watches WHERE plan_node_id=? AND status='armed'",
                     node_id))


def _library_owner(thread_id: str) -> None:
    models.register_artifact(integration=library.LIBRARY_INTEGRATION,
                             type=library.INSTALLED_SKILL_TYPE, external_id="owner/pdf",
                             parent_thread_id=thread_id)


def _library_changed(manifest: str) -> dict:
    return {
        "event_type": library.ITEM_CHANGED_EVENT, "external_id": "owner/pdf", "name": "pdf",
        "installed_version": "1.0.0", "library_version": "1.1.0", "item_manifest": manifest,
        "library_index_generation": 1, "installed_at": "2026-10-19T00:00:00+00:00",
        "changed_files": ["SKILL.md"], "edited": False, "withdrawn_reason": None,
    }


def _email(account: str, message: str) -> dict:
    return {"account_id": account, "from": "sender@example.test", "to": "me@example.test",
            "thread_id": f"google:{account}:thread:t1", "message_id": message,
            "subject": "hello"}


# -- A rule on one's own PR merging -------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_self_rule_on_a_pr_merging_wakes_its_owner_with_the_note(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("other")
    register_pr(REPO, 88, "owner")
    rule = await _tool(
        "owner", operation="create", connector="github", event_type="pr.merged",
        match=[{"field": "repo", "op": "eq", "value": REPO},
               {"field": "pr_number", "op": "eq", "value": 88}],
        note="Merged: write the release note and close the plan task.",
    )
    assert (rule["to"], rule["mode"], rule["action"]) == ("thread:owner", "wake", "deliver")

    refused = await _tool("other", operation="create", connector="github",
                          event_type="pr.merged", to="thread:owner", note="wake them")
    assert refused.startswith("Refused: Your rules deliver only to you")
    assert len(_rows("SELECT id FROM event_rules WHERE origin LIKE 'agent:%'")) == 1

    result = await route(MERGED, _merged(88))

    assert result.outcome == RouteOutcome.ACCEPTED
    [(thread, content)] = sent
    assert thread == "owner"
    assert '"rule_notes": [\n    "Merged: write the release note and close the plan task."\n  ]' in content
    # The note rode the owner's own wake; no second delivery.
    assert _wakes("owner") == []
    [decision] = _decisions()
    assert (decision["rule_id"], decision["thread_id"], decision["outcome"], decision["via"]) == (
        rule["id"], "owner", "woke", "routed")

    explained = await _tool("owner", operation="explain", event_id=decision["event_id"])
    [why] = explained["decisions"]
    assert why["explanation"] == (
        f"rule {rule['id']} woke owner (with the delivery routing already made)")


@pytest.mark.asyncio
async def test_without_a_matching_rule_the_router_delivers_exactly_as_before(isolated_db, sent):
    _project(P)
    _agent("owner")
    register_pr(REPO, 89, "owner")
    await _tool("owner", operation="create", connector="github", event_type="pr.merged",
                match=[{"field": "pr_number", "op": "eq", "value": 1}], note="not this one")

    await route(MERGED, _merged(89))
    with_rule_table = sent[0][1]
    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None):
        register_pr(REPO, 90, "owner")
        await route(MERGED, _merged(90))

    assert "rule_notes" not in with_rule_table
    assert with_rule_table.replace("#89", "#N").replace("89", "N") == (
        sent[1][1].replace("#90", "#N").replace("90", "N"))
    assert _decisions() == []


def _purge(thread_id: str) -> None:
    _write("UPDATE threads SET lifecycle_state='purged' WHERE id=?", thread_id)


@pytest.mark.asyncio
async def test_a_self_rule_fires_on_a_pr_routing_gave_no_thread(isolated_db, sent, resolved):
    _project(P, conductor="cond")
    _agent("cond")
    _agent("a")
    _agent("b")
    rule = await _tool("a", operation="create", connector="github", event_type="pr.merged",
                       match=[{"field": "repo", "op": "eq", "value": REPO}], note="Unowned PR merged.")
    # A rule for someone else can't see an event no owner claims, nor can a
    # self-rule that doesn't name the event's repository.
    other = await _tool("cond", operation="create", connector="github", event_type="pr.merged",
                        to="thread:b", mode="digest")
    unnamed = await _tool("b", operation="create", connector="github", event_type="pr.merged",
                          note="Any PR merged.")

    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None):
        today = await route(MERGED, _merged(41))
    resolved.clear()
    result = await route(MERGED, _merged(42))

    # Today's result and its dead-letter record are unchanged; the rule only adds a wake.
    assert (result.outcome, result.thread_id, result.reason) == (
        today.outcome, today.thread_id, today.reason)
    assert result.outcome == RouteOutcome.DEAD_LETTERED
    parked = _rows("SELECT external_id, reason FROM dead_letter_events ORDER BY id")
    assert [row["reason"] for row in parked] == [today.reason, today.reason]
    assert sent == []
    [wake] = _wakes("a")
    assert "Unowned PR merged." in wake
    [decision] = _decisions()
    assert (decision["rule_id"], decision["thread_id"], decision["outcome"], decision["via"]) == (
        rule["id"], "a", "woke", "rule")
    assert _decisions(rule_id=other["id"]) == [] and _notes("b") == []
    assert _decisions(rule_id=unnamed["id"]) == [] and _wakes("b") == []
    # The projects tracking the repository were read once, outside any transaction.
    assert resolved == [False]


@pytest.mark.asyncio
async def test_a_rule_fires_when_routing_dead_letters_for_an_inactive_owner(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("a")
    for number in (41, 42):
        register_pr(REPO, number, "owner")
    rule = await _tool("a", operation="create", connector="github", event_type="pr.merged",
                       match=[{"field": "repo", "op": "eq", "value": REPO}], note="PR merged.")
    to_owner = await _tool("owner", operation="create", connector="github",
                           event_type="pr.merged", to="registered_owner")
    _purge("owner")

    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None):
        today = await route(MERGED, _merged(41))
    result = await route(MERGED, _merged(42))

    # Routing dead-letters for its inactive owner and keeps doing so; the rules,
    # scoped by that owner's project, only add a wake.
    assert (result.outcome, result.thread_id, result.reason) == (
        today.outcome, today.thread_id, today.reason)
    assert result.outcome == RouteOutcome.DEAD_LETTERED
    parked = _rows("SELECT external_id, reason FROM dead_letter_events ORDER BY id")
    assert [row["reason"] for row in parked] == [today.reason, today.reason]
    assert sent == []
    [wake] = _wakes("a")
    assert "PR merged." in wake
    [decision] = _decisions(rule_id=rule["id"])
    assert (decision["thread_id"], decision["outcome"], decision["via"]) == ("a", "woke", "rule")
    [owner_rule] = _decisions(rule_id=to_owner["id"])
    assert (owner_rule["outcome"], owner_rule["reason"]) == ("dead_lettered", "creator gone")
    assert _rows("SELECT enabled FROM event_rules WHERE id=?", to_owner["id"])[0]["enabled"] == 0


@pytest.mark.asyncio
async def test_webhook_reads_when_routing_chose_no_thread(isolated_db, sent, resolved, monkeypatch):
    from events import rules

    _project(P)
    _project(Q)
    _agent("a")
    _agent("q", Q)
    rows: list[str] = []
    real = rules.from_row
    monkeypatch.setattr("events.rules.from_row", lambda row: rows.append(row["scope"]) or real(row))

    async def run(number: int, repo: str = REPO) -> tuple[int, int]:
        """DB reads, and calls to the repository lookup (a policy query and git)."""
        rows.clear()
        resolved.clear()
        with statements() as seen:
            await route(MERGED, _merged(number, repo=repo))
        return len(read_statements(seen)), len(resolved)

    with _without_rules():
        await run(60)
        before, _ = await run(61)
    no_rule = await run(62)
    await _tool("a", operation="create", connector="github", event_type="pr.merged",
                match=[{"field": "repo", "op": "eq", "value": REPO}])
    await _tool("q", operation="create", connector="github", event_type="pr.merged",
                match=[{"field": "repo", "op": "eq", "value": "q/r"}])
    matched = await run(63)
    matched_rows = list(rows)
    untracked = await run(64, "nobody/tracks")

    # With no rule for the type, one read, no repository lookup and no origin check.
    assert no_rule == (before + 1, 0)
    assert _decisions(thread_id="a")[-1]["outcome"] == "woke"
    # Only the tracking project's rules are read: Q's rule for another repository is not.
    assert matched_rows == [f"project:{P}"] * 2  # the read-only check, then evaluation
    assert matched[1] == 1
    # A rule exists, so the outside author's one origin check runs (O1: the agents'
    # account row and its person row).
    ORIGIN_READS = 2
    assert untracked == (before + 1 + ORIGIN_READS, 1) and rows == []
    print(f"unrouted webhook (DB reads, lookups): dev=({before}, 0) "
          f"no_rule=+({no_rule[0] - before}, {no_rule[1]}) "
          f"matched=+({matched[0] - before}, {matched[1]}) "
          f"untracked=+({untracked[0] - before}, {untracked[1]})")


@pytest.mark.asyncio
async def test_an_unrouted_event_with_no_self_rule_never_looks_up_its_repository(
        isolated_db, sent, resolved):
    _project(P, conductor="cond")
    _agent("cond")
    _agent("a")
    _agent("b")
    # Rules that can't fire on an event no one routed: another type, someone
    # else's recipient, and a disabled self-rule.
    made = [
        await _tool("a", operation="create", connector="github", event_type="pr.synchronize",
                    match=[{"field": "repo", "op": "eq", "value": REPO}]),
        await _tool("cond", operation="create", connector="github", event_type="pr.merged",
                    to="thread:b", mode="digest"),
        await _tool("a", operation="create", connector="github", event_type="pr.merged",
                    match=[{"field": "repo", "op": "eq", "value": REPO}]),
    ]
    assert all(isinstance(rule, dict) for rule in made), made
    assert (await _tool("a", operation="disable", rule_id=made[2]["id"]))["enabled"] is False
    resolved.clear()

    await route(MERGED, _merged(70))

    assert resolved == [] and _decisions() == []
    # Both probes of that read, and the project-scoped lookup, search the one type index.
    from events import rule_reads

    probes = (f"SELECT EXISTS(SELECT 1 {rule_reads._TYPE_RULES}), "
              f"EXISTS(SELECT 1 {rule_reads._SELF_RULES})")
    scans = [row["detail"] for row in _rows(f"EXPLAIN QUERY PLAN {probes}", *("github", "pr.merged") * 2)
             if "event_rules" in row["detail"]]
    assert len(scans) == 2 and all("INDEX idx_event_rules_type (connector=?" in s for s in scans)
    [plan, *_] = _rows(
        "EXPLAIN QUERY PLAN SELECT * FROM event_rules WHERE scope=? || (SELECT project_id FROM "
        "threads WHERE id=?) AND connector=? AND event_type=? AND enabled=1 ORDER BY created_at, id",
        "project:", "a", "github", "pr.merged")
    assert "USING INDEX idx_event_rules_type (connector=?" in plan["detail"]


# -- Burst -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_burst_wakes_a_rule_and_the_owner_on_every_event(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    rule = await _tool("watcher", operation="create", connector="github",
                       event_type="pr.synchronize",
                       match=[{"field": "repo", "op": "eq", "value": REPO}],
                       note="Re-run the load check.")

    for n in range(50):
        assert (await route(SYNC, _sync(7, n))).outcome == RouteOutcome.ACCEPTED
    assert (await route("webhook.github.pr_comment", _comment(7))).outcome == RouteOutcome.ACCEPTED

    # The owner's routing is untouched: every push and the comment wake it.
    assert [thread for thread, _ in sent] == ["owner"] * 51
    assert all("rule_notes" not in content for _, content in sent)
    wakes = _wakes("watcher")
    assert len(wakes) == 50
    assert all("Re-run the load check." in wake for wake in wakes)
    assert _digest("watcher") == []
    assert [(d["outcome"], d["via"], d["reason"]) for d in _decisions(rule_id=rule["id"])] == (
        [("woke", "rule", None)] * 50)

    [listed] = (await _tool("watcher", operation="list"))["rules"]
    assert listed["wakes_24h"] == 50
    assert listed["last_fired"] is not None


# -- Writer limits ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_writer_limits(isolated_db, sent, resolved, monkeypatch):
    _project(P, conductor="cond")
    _project(Q)
    _agent("cond")
    _agent("a")
    _agent("b")
    _agent("other")
    _agent("worker", temporary_of="a")
    base = {"operation": "create", "connector": "github", "event_type": "pr.merged"}

    other = await _tool("a", **base, to="thread:b")
    assert other.startswith("Refused: Your rules deliver only to you")
    assert "Ask the project's conductor (cond)" in other
    unreadable = await _tool("a", **base, scope=f"project:{Q}")
    assert unreadable.startswith("Refused: You can only write rules about events you can already read")
    assert "drop removes only deliveries to you" in await _tool("a", **base, action="drop",
                                                                to="thread:b")
    assert "temporary agent can't write rules" in await _tool("worker", **base)
    assert "no field 'title'" in await _tool(
        "a", **base, match=[{"field": "title", "op": "eq", "value": "x"}])
    # A repository test names one of the writer's project's repositories.
    for test in ({"field": "repo", "op": "eq", "value": "q/r"},
                 {"field": "repo", "op": "in", "value": [REPO, "q/r"]}):
        assert await _tool("a", **base, match=[test]) == (
            "Refused: q/r isn't one of this project's repositories (o/r, x/y).")
    # The lookup runs git, so it runs before the write transaction opens.
    assert resolved == [False, False]

    def _unreadable(project_id=None):
        from events.connectors.github_notifications_feed import FeedError
        raise FeedError("repo_scope_unavailable")

    with monkeypatch.context() as patched:
        patched.setattr("events.connectors.github_notifications_feed.tracked_github_repositories",
                        _unreadable)
        assert await _tool("a", **base, match=[{"field": "repo", "op": "eq", "value": REPO}]) == (
            "Refused: This project's repositories can't be read right now; try again.")
    assert "role:" not in json.dumps(await _tool("a", **base, to="registered_owner"))

    # The conductor may give another agent in its project a digest, nothing more.
    assert "digest only" in await _tool("cond", **base, to="thread:b", mode="wake")
    assert "temporary agent can't receive" in await _tool("cond", **base, to="thread:worker",
                                                          mode="digest")
    digest = await _tool("cond", **base, to="thread:b", mode="digest")
    assert digest["to"] == "thread:b"

    assert "Only the rule's writer" in await _tool("a", operation="disable", rule_id=digest["id"])
    assert (await _tool("b", operation="disable", rule_id=digest["id"]))["enabled"] is False

    registered = await _tool("a", **base, to="registered_owner")
    register_pr(REPO, 4, "b")
    await route(MERGED, _merged(4))
    assert "Only the rule's writer" in await _tool("other", operation="disable", rule_id=registered["id"])
    assert (await _tool("b", operation="disable", rule_id=registered["id"]))["enabled"] is False

    # Every declared field can match: a PR's author comes from the webhook.
    by_author = await _tool("a", **base, match=[{"field": "author", "op": "eq", "value": "carol"}])
    register_pr(REPO, 5, "a")
    register_pr(REPO, 6, "a")
    await route(MERGED, _merged(5, author="carol"))
    await route(MERGED, _merged(6, author="dave"))
    [hit] = _decisions(rule_id=by_author["id"])
    assert (hit["thread_id"], hit["outcome"]) == ("a", "woke")
    assert hit["artifact_external_id"] == pr_external_id(REPO, 5)
    assert len(_rows("SELECT id FROM event_rules WHERE origin LIKE 'agent:%'")) == 4


@pytest.mark.asyncio
async def test_test_matches_only_the_callers_project(isolated_db):
    _project(P)
    _project(Q)
    _agent("a")
    _agent("b", Q)
    rule = await _tool("a", operation="create", connector="github", event_type="pr.merged",
                       match=[{"field": "repo", "op": "prefix", "value": "o/"}])
    facts = {"repo": REPO, "pr_number": 3}

    [hit] = (await _tool("a", operation="test", connector="github", event_type="pr.merged",
                         facts=facts))["matches"]
    assert (hit["id"], hit["would"]) == (rule["id"], "wake a")
    assert (await _tool("b", operation="test", connector="github", event_type="pr.merged",
                        facts=facts))["matches"] == []
    assert (await _tool("a", operation="test", connector="github", event_type="pr.merged",
                        facts={"repo": "x/y"}))["matches"] == []


# -- Drops, holds and guards -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_drop_removes_the_writers_own_delivery_and_leaves_its_watch_armed(isolated_db, sent):
    _project(P)
    _agent("owner")
    register_pr(REPO, 7, "owner")
    node, watch = _watch("owner", pr_external_id(REPO, 7), SYNC)
    rule = await _tool("owner", operation="create", connector="github",
                       event_type="pr.synchronize", action="drop")

    result = await route(SYNC, _sync(7, 1))

    assert (result.outcome, result.reason) == (RouteOutcome.SUPPRESSED, f"dropped by rule {rule['id']}")
    assert sent == []
    assert _armed(node) == 1
    [why] = (await _tool("owner", operation="explain", artifact=pr_external_id(REPO, 7)))["decisions"]
    assert why["outcome"] == "dropped"
    assert why["armed_watches_left_waiting"] == [watch]
    assert f"armed watch {watch} still waits" in why["explanation"]


@pytest.mark.asyncio
async def test_rules_hold_threads_that_left_their_project(isolated_db, sent, monkeypatch):
    _project(P, conductor="cond")
    _project(Q)
    _agent("cond")
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    rule = await _tool("cond", operation="create", connector="github",
                       event_type="pr.synchronize", to="thread:watcher", mode="digest")
    _write("UPDATE threads SET project_id=? WHERE id='watcher'", Q)

    await route(SYNC, _sync(7, 1))

    assert _wakes("watcher") == [] and _notes("watcher") == []
    [held] = _decisions(rule_id=rule["id"])
    assert (held["outcome"], held["reason"]) == ("held", "recipient outside the rule's project")
    [why] = (await _tool("cond", operation="explain", event_id=held["event_id"]))["decisions"]
    assert why["explanation"].endswith("held it from watcher: recipient outside the rule's project")

    _write("UPDATE threads SET project_id=? WHERE id='watcher'", P)
    await route(SYNC, _sync(7, 2))
    assert len(_notes("watcher")) == 1

    drop = await _tool("owner", operation="create", connector="gmail",
                       event_type="email.received", action="drop")
    _write("UPDATE threads SET project_id=? WHERE id='owner'", Q)
    models.register_artifact(integration="gmail", type="email-thread",
                             external_id="google:acct-1:thread:t1", parent_thread_id="owner")
    [delivered] = deliver_platform_event_sync("gmail", _email("acct-1", "moved-writer"))
    # The event is now the owner's new project's; the old project's drop never sees it.
    assert delivered.outcome == "digested"
    assert _decisions(rule_id=drop["id"]) == []


@pytest.mark.asyncio
async def test_r29_a_cloud_off_projects_events_reach_no_cloud_recipient_outside_it(isolated_db, sent):
    """R29 holds by construction in 2.A: a rule's recipient must be in the rule's
    project, which is the event's, and no scope can cross projects."""
    import sqlite3

    _project(P, conductor="cond", cloud_off=True)
    _project(Q)
    _agent("cond", local_only=True)
    _agent("owner", local_only=True)
    _agent("cloud", local_only=True)
    register_pr(REPO, 7, "owner")
    rule = await _tool("cond", operation="create", connector="github",
                       event_type="pr.synchronize", to="thread:cloud", mode="digest")
    # The recipient leaves for a project that runs cloud models.
    _write("UPDATE threads SET project_id=?, local_only=0 WHERE id='cloud'", Q)

    await route(SYNC, _sync(7, 1))

    assert _wakes("cloud") == [] and _notes("cloud") == [] and _digest("cloud") == []
    assert [(d["outcome"], d["reason"]) for d in _decisions(rule_id=rule["id"])] == [
        ("held", "recipient outside the rule's project")]
    with pytest.raises(sqlite3.IntegrityError):  # no rule scope spans projects
        _write("UPDATE event_rules SET scope='global' WHERE id=?", rule["id"])


@pytest.mark.asyncio
async def test_disabled_rules_age_out_on_the_next_write(isolated_db):
    _project(P)
    _agent("writer")
    base = {"operation": "create", "connector": "github", "event_type": "pr.merged"}
    old = await _tool("writer", **base)
    recent = await _tool("writer", **base)
    await _tool("writer", operation="disable", rule_id=old["id"])
    await _tool("writer", operation="disable", rule_id=recent["id"])
    _write("UPDATE event_rules SET updated_at=? WHERE id=?", "2026-08-01T00:00:00+00:00", old["id"])

    await _tool("writer", **base)

    assert {row["id"] for row in _rows("SELECT id FROM event_rules")} >= {recent["id"]}
    assert old["id"] not in {row["id"] for row in _rows("SELECT id FROM event_rules")}

    stale = await _tool("writer", **base)
    await _tool("writer", operation="disable", rule_id=stale["id"])
    _write("UPDATE event_rules SET updated_at=? WHERE id=?", "2026-08-01T00:00:00+00:00", stale["id"])
    enabled = await _tool("writer", **base)
    await _tool("writer", operation="disable", rule_id=enabled["id"])
    assert stale["id"] not in {row["id"] for row in _rows("SELECT id FROM event_rules")}


@pytest.mark.asyncio
async def test_a_paused_target_gets_a_digest(isolated_db, sent, monkeypatch):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    rule = await _tool("watcher", operation="create", connector="github",
                       event_type="pr.synchronize")
    monkeypatch.setattr("plans.public.thread_paused_since",
                        lambda _db, thread: "2026-10-21T00:00:00+00:00" if thread == "watcher" else None)

    await route(SYNC, _sync(7, 1))

    assert (_wakes("watcher"), len(_notes("watcher"))) == ([], 1)
    assert [(d["outcome"], d["reason"]) for d in _decisions(rule_id=rule["id"])] == [
        ("digested", "paused")]


@pytest.mark.asyncio
async def test_an_ended_pr_fires_no_rule_but_its_end_event_does(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    merged = await _tool("watcher", operation="create", connector="github", event_type="pr.merged")
    pushed = await _tool("watcher", operation="create", connector="github",
                         event_type="pr.synchronize")

    await route(MERGED, _merged(7))
    await route(SYNC, _sync(7, 2))

    assert len(_wakes("watcher")) == 1
    assert [d["outcome"] for d in _decisions(rule_id=merged["id"])] == ["woke"]
    # The owner still gets the later push from routing; the rule records why it didn't fire.
    assert [d["outcome"] for d in _decisions(rule_id=pushed["id"])] == ["ended"]
    assert [thread for thread, _ in sent] == ["owner", "owner"]
    assert _notes("watcher") == []


@pytest.mark.asyncio
async def test_todays_drops_still_come_before_any_rule(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    await _tool("watcher", operation="create", connector="github", event_type="check.completed")
    await _tool("watcher", operation="create", connector="github", event_type="pr.synchronize")

    cancelled = check_suite_payload(REPO, 7, conclusion="cancelled", base_ref="dev")
    cancelled["check_suite"]["head_sha"] = "sha-c"
    await route("webhook.github.check_suite_completed", cancelled)
    own = await route(SYNC, _sync(7, 3, author="corsia[bot]", author_type="Bot"))

    assert own.outcome == RouteOutcome.SUPPRESSED
    assert sent == []
    assert (_wakes("watcher"), _notes("watcher"), _decisions()) == ([], [], [])


def _suite(number: int) -> dict:
    payload = check_suite_payload(REPO, number, base_ref="dev")
    payload["check_suite"]["head_sha"] = "sha-s"
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize(("internal_event", "payload"), [
    (SYNC, _sync(7, 1)), ("webhook.github.check_suite_completed", _suite(7))])
async def test_the_rule_events_artifact_is_the_one_routing_resolved(
        isolated_db, sent, monkeypatch, internal_event, payload):
    from events import webhook_rules

    monkeypatch.setattr("reviewer.gh_helpers.get_prs_for_sha", lambda *_args: [
        {"number": 7, "state": "open", "base_ref": "dev", "head_ref": "f", "head_sha": "sha-s"}])

    _project(P)
    _agent("owner")
    register_pr(REPO, 7, "owner")
    for event_type in ("pr.synchronize", "check.completed"):  # rules are read only when one exists
        await _tool("owner", operation="create", connector="github", event_type=event_type,
                    match=[{"field": "repo", "op": "eq", "value": "x/y"}])
    seen: list[tuple[dict | None, dict | None]] = []
    real = webhook_rules.apply_webhook_rules_sync

    def capture(envelope, watch_event, targets, **kwargs):
        identity = next(t.routing_state for t in targets if t.routing_state)["identity"]
        seen.append((envelope.artifact_ref, identity))
        return real(envelope, watch_event, targets, **kwargs)

    with patch("events.connectors.github_rules.apply_webhook_rules_sync", capture):
        await route(internal_event, payload)

    [(artifact_ref, identity)] = seen
    assert identity is not None
    assert artifact_ref == {key: identity[key] for key in ("integration", "type", "external_id")}


def test_r30_the_mail_pollers_reach_filter_holds_rule_recipients(isolated_db):
    import asyncio

    from connections.connected_accounts import account_reaches_thread

    _project(P)
    _agent("owner")
    _agent("watcher")
    models.register_artifact(integration="gmail", type="email-thread",
                             external_id="google:acct-1:thread:t1", parent_thread_id="owner")
    asyncio.run(_tool("watcher", operation="create", connector="gmail",
                      event_type="email.received"))

    def inside_account_reach(target, connection):  # the mail poller's own filter
        if target.thread is None or account_reaches_thread("acct-1", target.thread, db=connection):
            return None
        return "account_out_of_reach"

    # An unknown account reaches no one: routing's owner and the rule's recipient alike.
    deliveries = deliver_platform_event_sync("gmail", _email("acct-1", "m1"),
                                             target_filter=inside_account_reach)
    assert {(d.thread_id, d.outcome, d.reason) for d in deliveries} == {
        ("owner", FILTERED, "account_out_of_reach"), ("watcher", FILTERED, "account_out_of_reach")}
    [held] = _decisions()
    assert (held["thread_id"], held["outcome"], held["reason"]) == (
        "watcher", "held", "account_out_of_reach")
    [why] = asyncio.run(_tool("watcher", operation="explain",
                              event_id=held["event_id"]))["decisions"]
    assert why["explanation"].endswith("held it from watcher: account_out_of_reach")


@pytest.mark.asyncio
async def test_rule_authoring_and_both_delivery_paths_emit_product_events(isolated_db, sent, monkeypatch):
    authored, routed = [], []
    monkeypatch.setattr("tools.event_rules.emit_product_event",
                        lambda category, action, **kwargs: authored.append((category, action, kwargs)))
    monkeypatch.setattr("events.platform.emit_product_event",
                        lambda category, action, **kwargs: routed.append((category, action, kwargs)))
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")

    webhook_rule = await _tool("watcher", operation="create", connector="github",
                               event_type="pr.synchronize")
    disabled = await _tool("watcher", operation="create", connector="github",
                           event_type="pr.merged")
    await _tool("watcher", operation="disable", rule_id=disabled["id"])
    assert [(category, action) for category, action, _ in authored] == [
        ("event_rule", "created"), ("event_rule", "created"), ("event_rule", "disabled")]

    await route(SYNC, _sync(7, 1))
    webhook_events = [
        (category, action, event["properties"]["thread_id"])
        for category, action, event in routed if category == "event_route"
    ]
    assert webhook_events == [("event_route", "woke", "watcher")]

    _library_owner("owner")
    platform_rule = await _tool("watcher", operation="create", connector=library.LIBRARY_CONNECTOR,
                                event_type=library.ITEM_CHANGED_EVENT)
    delivered = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))
    assert platform_rule["id"]
    assert any(category == "event_route" and action == "woke"
               and event["properties"]["thread_id"] == "watcher"
               for category, action, event in routed)
    assert {delivery.thread_id for delivery in delivered} == {"owner", "watcher"}


# -- Platform path and watches ----------------------------------------------------------------

def test_a_watch_ends_only_where_a_rule_added_the_delivery(isolated_db, monkeypatch):
    import asyncio

    from events import resolve

    _project(P)
    _agent("owner")
    _library_owner("owner")
    node, _watch_id = _watch("owner", "owner/pdf", library.ITEM_CHANGED_EVENT)

    # Today: the owner's digest of this event does not end the wait.
    [plain] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))
    assert plain.outcome == DIGESTED and _armed(node) == 1

    asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                      event_type=library.ITEM_CHANGED_EVENT, note="Reread the skill."))
    [ridden] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-2"))

    # The rule rides routing's delivery: its wake beats the digest and carries its
    # note, and the wait stays routing's to end.
    assert ridden.outcome == "woke"
    [wake] = _wakes("owner")
    assert "Reread the skill." in wake
    assert _armed(node) == 1

    real = resolve.resolve_targets

    def _no_thread(envelope, **kwargs):
        [target] = real(envelope, **kwargs)
        return [resolve.Target(None, "dead_letter", "none", routing_state=target.routing_state,
                               disposition="dead_letter")]

    monkeypatch.setattr("events.platform.resolve_targets", _no_thread)
    made = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-3"))

    # Routing chose no thread, so the rule added a wake and that delivery ends the wait.
    assert ("owner", "woke") in {(d.thread_id, d.outcome) for d in made}
    assert _armed(node) == 0


def test_event_rule_schema_matches_rules():
    from events import rules
    from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL

    operators = ",".join(f"'{op}'" for op in rules.OPERATORS)
    assert f"IN ({operators})" in EVENT_RULE_SCHEMA_SQL
    assert f"json_array_length(\"match\") <= {rules.MAX_TESTS})" in EVENT_RULE_SCHEMA_SQL
    assert f"$[{rules.MAX_TESTS - 1}]" in EVENT_RULE_SCHEMA_SQL
    assert f"$[{rules.MAX_TESTS}]" not in EVENT_RULE_SCHEMA_SQL
    assert f"length(note) <= {rules.NOTE_LIMIT})" in EVENT_RULE_SCHEMA_SQL


def test_repeated_rule_wakes_override_routed_digest(isolated_db):
    import asyncio

    _project(P)
    _agent("owner")
    _library_owner("owner")
    asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                      event_type=library.ITEM_CHANGED_EVENT, note="Reread the skill."))

    [first] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))
    [second] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-2"))

    assert (first.outcome, second.outcome) == ("woke", "woke")
    assert len(_wakes("owner")) == 2
    assert _notes("owner") == []


def test_a_platform_drop_filters_the_writers_delivery_and_keeps_the_wait(isolated_db):
    import asyncio

    _project(P)
    _agent("owner")
    _library_owner("owner")
    node, _watch_id = _watch("owner", "owner/pdf", library.ITEM_CHANGED_EVENT)
    rule = asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                             event_type=library.ITEM_CHANGED_EVENT, action="drop"))

    [dropped] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))

    assert (dropped.outcome, dropped.reason) == (FILTERED, f"dropped by rule {rule['id']}")
    assert _notes("owner") == [] and _armed(node) == 1


# -- Scale: reads added per event ------------------------------------------------------------

@contextmanager
def _without_rules():
    """The webhook router as it was before rules: no read before the gate, none in it."""
    from events.connectors.github_rules import RuleScope

    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None), \
            patch("events.connectors.github_rules.rule_scope",
                  lambda *a: RuleScope(any_rule=False)):
        yield


def test_platform_reads_per_event(isolated_db, monkeypatch):
    import asyncio

    _project(P)
    _agent("owner")
    _agent("watcher")
    _library_owner("owner")

    def run(manifest: str) -> list[str]:
        with statements() as seen:
            deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed(manifest))
        return read_statements(seen)

    with patch("events.platform.rules.evaluate", lambda *_args, **_kwargs: None):
        before = run("warm")  # warms one-time reads
        before = run("m-0")   # dev's reads: no rule evaluation at all
    no_rule = run("m-1")
    asyncio.run(_tool("watcher", operation="create", connector=library.LIBRARY_CONNECTOR,
                      event_type=library.ITEM_CHANGED_EVENT,
                      match=[{"field": "name", "op": "eq", "value": "other"}]))
    unmatched = run("m-2")
    asyncio.run(_tool("watcher", operation="create", connector=library.LIBRARY_CONNECTOR,
                      event_type=library.ITEM_CHANGED_EVENT, mode="digest"))
    matched = run("m-3")

    added = [s for s in no_rule if "event_rules" in s]
    assert len(no_rule) - len(before) == 1 and len(added) == 1
    assert len(unmatched) - len(before) == 1
    print(f"platform reads: dev={len(before)} no_rule=+{len(no_rule) - len(before)} "
          f"unmatched=+{len(unmatched) - len(before)} matched=+{len(matched) - len(before)}")


@pytest.mark.asyncio
async def test_webhook_reads_per_event(isolated_db, sent):
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")

    async def run(n: int) -> list[str]:
        with statements() as seen:
            await route(SYNC, _sync(7, n))
        return read_statements(seen)

    with _without_rules():
        await run(0)
        before = await run(1)
    no_rule = await run(2)
    await _tool("watcher", operation="create", connector="github", event_type="pr.synchronize",
                match=[{"field": "repo", "op": "eq", "value": "x/y"}])
    unmatched = await run(3)
    await _tool("watcher", operation="create", connector="github", event_type="pr.synchronize")
    matched = await run(4)

    # No rule for the type: the one read before the gate, and none in it.
    assert len(no_rule) - len(before) == 1
    assert len([s for s in no_rule if "event_rules" in s]) == 1
    # A rule for the type: that read, then its project's rules.
    assert len(unmatched) - len(before) == 2
    print(f"webhook reads: dev={len(before)} no_rule=+{len(no_rule) - len(before)} "
          f"unmatched=+{len(unmatched) - len(before)} matched=+{len(matched) - len(before)}")


# -- Rules read routing's decision and its owners' facts ---------------------------------------

@pytest.mark.asyncio
async def test_rules_read_the_registrys_ended_state(isolated_db, sent):
    """Ended is the registry's one definition: the identity's recorded PR state."""
    _project(P)
    _agent("owner")
    _agent("watcher")
    register_pr(REPO, 7, "owner")
    rule = await _tool("watcher", operation="create", connector="github",
                       event_type="pr.synchronize")
    _write("UPDATE artifact_identities SET metadata=json_set(COALESCE(metadata, '{}'), "
           "'$.pr_state', 'closed') WHERE external_id=?", pr_external_id(REPO, 7))

    await route(SYNC, _sync(7, 1))

    assert [d["outcome"] for d in _decisions(rule_id=rule["id"])] == ["ended"]
    assert _wakes("watcher") == [] and _notes("watcher") == []


@pytest.mark.parametrize("relationship, lifecycle", [("owner", "archived"), ("parent_chain", "active")])
def test_a_rule_wake_rides_only_where_the_disposition_table_allows(
        isolated_db, monkeypatch, relationship, lifecycle):
    import asyncio

    from events import resolve
    from events.resolve import Target

    _project(P)
    _agent("owner")
    _library_owner("owner")
    asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                      event_type=library.ITEM_CHANGED_EVENT, note="Reread the skill."))
    real = resolve.resolve_targets

    def _targets(envelope, **kwargs):
        [target] = real(envelope, **kwargs)
        state = dict(target.routing_state)
        state["identity"] = {**state["identity"], "state": lifecycle}
        return [Target(target.thread, target.resolution, relationship, routing_state=state,
                       disposition="owner_digest")]

    monkeypatch.setattr("events.platform.resolve_targets", _targets)
    [made] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))

    assert made.outcome == DIGESTED
    assert _wakes("owner") == []


def test_rule_adds_pass_the_callers_filter_and_project_scope(isolated_db):
    _project(P)
    _project(Q)
    _agent("owner")
    _agent("watcher")
    _agent("outsider", Q)
    models.register_artifact(integration="gmail", type="email-thread",
                             external_id="google:acct-1:thread:t1", parent_thread_id="owner")
    import asyncio

    inside = asyncio.run(_tool("watcher", operation="create", connector="gmail",
                               event_type="email.received"))
    outside = asyncio.run(_tool("outsider", operation="create", connector="gmail",
                                event_type="email.received"))

    def _reach(target, _conn):
        return "account_out_of_reach" if target.thread == "watcher" else None

    deliveries = deliver_platform_event_sync("gmail", _email("acct-1", "m1"), target_filter=_reach)

    assert ("watcher", FILTERED, "account_out_of_reach") in {
        (d.thread_id, d.outcome, d.reason) for d in deliveries}
    assert [(d["outcome"], d["reason"]) for d in _decisions(rule_id=inside["id"])] == [
        ("held", "account_out_of_reach")]
    # Another project's rule never sees this project's event.
    assert _decisions(rule_id=outside["id"]) == []
    assert _wakes("watcher") == [] and _wakes("outsider") == []


def test_platform_routed_delivery_ends_the_same_watches_with_or_without_a_rule(isolated_db):
    import asyncio

    _project(P)
    _agent("owner")
    _library_owner("owner")
    without_node, _ = _watch("owner", "owner/pdf", library.ITEM_CHANGED_EVENT)
    deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))
    without = _armed(without_node)

    with_node, _ = _watch("owner", "owner/pdf", library.ITEM_CHANGED_EVENT)
    asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                      event_type=library.ITEM_CHANGED_EVENT, mode="digest", note="Reread it."))
    deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-2"))

    assert (without, _armed(with_node)) == (1, 1)


def test_lookup_reads_only_the_event_projects_rules(isolated_db, monkeypatch):
    import asyncio

    from events import rules

    _project(P)
    _project(Q)
    _agent("owner")
    _agent("other", Q)
    _library_owner("owner")
    for n in range(5):
        made = asyncio.run(_tool("other", operation="create", connector=library.LIBRARY_CONNECTOR,
                                 event_type=library.ITEM_CHANGED_EVENT,
                                 mode="digest", note=f"note {n}"))
        assert isinstance(made, dict), made
    read: list[str] = []
    real = rules.from_row
    monkeypatch.setattr("events.rules.from_row", lambda row: read.append(row["scope"]) or real(row))

    deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))

    assert read == []


@pytest.mark.parametrize("owner, woke", [("worker", True), ("other", False)])
def test_an_agents_owner_rule_wakes_only_its_own_or_its_workers(isolated_db, owner, woke):
    import asyncio

    _project(P)
    _agent("writer")
    _agent("other")
    _agent("worker", temporary_of="writer")
    _library_owner(owner)
    rule = asyncio.run(_tool("writer", operation="create", connector=library.LIBRARY_CONNECTOR,
                             event_type=library.ITEM_CHANGED_EVENT, to="registered_owner",
                             note="Reread the skill."))

    [made] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))

    assert made.thread_id == owner
    assert (made.outcome == "woke", len(_wakes(owner)) == 1) == (woke, woke)
    [decision] = _decisions(rule_id=rule["id"])
    assert decision["outcome"] == ("woke" if woke else "digested")


def test_an_agents_owner_rule_does_not_wake_a_promoted_worker(isolated_db):
    import asyncio

    _project(P)
    _agent("writer")
    _agent("temporary", temporary_of="writer")
    _agent("promoted", temporary_of="writer")
    _write("UPDATE threads SET agent_enabled=1 WHERE id=?", "promoted")
    rule = asyncio.run(_tool("writer", operation="create", connector=library.LIBRARY_CONNECTOR,
                             event_type=library.ITEM_CHANGED_EVENT, to="registered_owner"))

    _library_owner("temporary")
    models.register_artifact(integration=library.LIBRARY_INTEGRATION,
                             type=library.INSTALLED_SKILL_TYPE, external_id="owner/promoted",
                             parent_thread_id="promoted")
    [temporary_delivery] = deliver_platform_event_sync(
        library.LIBRARY_CONNECTOR, _library_changed("temporary"))
    promoted_event = _library_changed("promoted") | {"external_id": "owner/promoted"}
    [promoted_delivery] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, promoted_event)

    assert (temporary_delivery.outcome, promoted_delivery.outcome) == ("woke", "digested")
    decisions = _decisions(rule_id=rule["id"])
    assert [(row["thread_id"], row["outcome"], row["reason"]) for row in decisions] == [
        ("temporary", "woke", None), ("promoted", "digested", "the registered owner is another agent")]


def test_a_drop_with_nothing_routed_records_nothing(isolated_db):
    import asyncio

    _project(P)
    _agent("owner")
    _agent("watcher")
    _library_owner("owner")
    drop = asyncio.run(_tool("watcher", operation="create", connector=library.LIBRARY_CONNECTOR,
                             event_type=library.ITEM_CHANGED_EVENT, action="drop"))

    deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))

    assert _decisions(rule_id=drop["id"]) == []
    [listed] = asyncio.run(_tool("watcher", operation="list"))["rules"]
    assert listed["last_fired"] is None
