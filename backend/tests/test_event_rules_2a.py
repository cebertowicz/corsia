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
from tests.event_routing_helpers import check_suite_payload, mk_session, pr_payload, register_pr, route
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

    monkeypatch.setattr("events.connectors.github_notifications_feed.tracked_github_repositories",
                        _tracked)
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


def _merged(number: int, author: str = "alice") -> dict:
    payload = pr_payload(REPO, number, action="closed")
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
    assert len(_rows("SELECT id FROM event_rules")) == 1

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


@pytest.mark.asyncio
async def test_a_self_rule_fires_on_a_pr_routing_gave_no_thread(isolated_db, sent):
    _project(P, conductor="cond")
    _agent("cond")
    _agent("a")
    _agent("b")
    rule = await _tool("a", operation="create", connector="github", event_type="pr.merged",
                       match=[{"field": "repo", "op": "eq", "value": REPO}], note="Unowned PR merged.")
    # A rule for someone else can't see an event no project claims, nor can a
    # self-rule that doesn't name the event's repository.
    other = await _tool("cond", operation="create", connector="github", event_type="pr.merged",
                        to="thread:b", mode="digest")
    unnamed = await _tool("b", operation="create", connector="github", event_type="pr.merged",
                          note="Any PR merged.")

    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None):
        today = await route(MERGED, _merged(41))
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


@pytest.mark.asyncio
async def test_webhook_reads_when_routing_chose_no_thread(isolated_db, sent):
    _project(P)
    _agent("a")

    async def run(number: int) -> list[str]:
        with _statements() as seen:
            await route(MERGED, _merged(number))
        return _reads(seen)

    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None):
        await run(60)
        before = await run(61)
    no_rule = await run(62)
    await _tool("a", operation="create", connector="github", event_type="pr.merged",
                match=[{"field": "repo", "op": "eq", "value": REPO}])
    matched = await run(63)

    assert len(no_rule) - len(before) == 1
    assert _decisions(thread_id="a")[-1]["outcome"] == "woke"
    print(f"unrouted webhook reads: dev={len(before)} no_rule=+{len(no_rule) - len(before)} "
          f"matched=+{len(matched) - len(before)}")


# -- Burst -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_burst_wakes_a_rule_once_and_the_owner_on_every_event(isolated_db, sent):
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
    [wake] = _wakes("watcher")
    assert "Re-run the load check." in wake
    # The 49 capped events reach the next turn as one note that counts them.
    [note] = _digest("watcher")
    assert f"; 49 since rule {rule['id']} last woke you" in note["summary"]
    assert note["summary"].endswith("; rule note: Re-run the load check.")
    assert [(d["outcome"], d["via"], d["reason"]) for d in _decisions(rule_id=rule["id"])] == (
        [("woke", "rule", None)] + [("digested", "rule", "wake cap")] * 49)

    [listed] = (await _tool("watcher", operation="list"))["rules"]
    assert listed["wakes_24h"] == 1
    assert listed["last_fired"] is not None


# -- Writer limits ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_writer_limits(isolated_db, sent, resolved, monkeypatch):
    _project(P, conductor="cond")
    _project(Q)
    _agent("cond")
    _agent("a")
    _agent("b")
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

    # Every declared field can match: a PR's author comes from the webhook.
    by_author = await _tool("a", **base, match=[{"field": "author", "op": "eq", "value": "carol"}])
    register_pr(REPO, 5, "a")
    register_pr(REPO, 6, "a")
    await route(MERGED, _merged(5, author="carol"))
    await route(MERGED, _merged(6, author="dave"))
    [hit] = _decisions(rule_id=by_author["id"])
    assert (hit["thread_id"], hit["outcome"]) == ("a", "woke")
    assert hit["artifact_external_id"] == pr_external_id(REPO, 5)
    assert len(_rows("SELECT id FROM event_rules")) == 3


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
    monkeypatch.setattr("events.rules._reaches", lambda *_args: True)
    [delivered] = deliver_platform_event_sync("gmail", _email("acct-1", "moved-writer"))
    assert delivered.outcome == "digested"
    assert [(item["outcome"], item["reason"]) for item in _decisions(rule_id=drop["id"])] == [
        ("held", "recipient outside the rule's project")]


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


def test_r29_and_r30_hold_an_account_event_from_recipients_it_may_not_reach(isolated_db, monkeypatch):
    import asyncio

    _project(P, cloud_off=True)
    _project(Q)
    _agent("owner")
    _agent("cloud", Q)
    _agent("local", Q, local_only=True)
    models.register_artifact(integration="gmail", type="email-thread",
                             external_id="google:acct-1:thread:t1", parent_thread_id="owner")
    for writer in ("cloud", "local"):
        asyncio.run(_tool(writer, operation="create", connector="gmail",
                          event_type="email.received"))

    # R30, read through the real reach check: an unknown account reaches no one.
    deliver_platform_event_sync("gmail", _email("acct-1", "m1"))
    assert {d["thread_id"]: (d["outcome"], d["reason"]) for d in _decisions()} == {
        "cloud": ("held", "account reach"), "local": ("held", "account reach")}

    # R29: with reach granted, a Local-models-only project's mail reaches only the local agent.
    monkeypatch.setattr("events.rules._reaches", lambda *_args: True)
    deliveries = deliver_platform_event_sync("gmail", _email("acct-1", "m2"))
    later = [d for d in _decisions() if d["id"] > 2]
    assert {d["thread_id"]: (d["outcome"], d["reason"]) for d in later} == {
        "cloud": ("held", "local only"), "local": ("woke", None)}
    assert "local" in {d.thread_id for d in deliveries}
    assert _wakes("cloud") == [] and len(_wakes("local")) == 1

    held = asyncio.run(_tool("cloud", operation="explain", event_id=later[0]["event_id"]))
    [why] = [d for d in held["decisions"] if d["outcome"] == "held"]
    assert why["explanation"].endswith("held it from cloud: local only")
    # The owner's project can't see the other project's rules.
    assert asyncio.run(_tool("owner", operation="explain",
                             event_id=later[0]["event_id"]))["decisions"] == []


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

def test_a_watch_ends_when_an_authored_rule_delivers_its_event(isolated_db):
    import asyncio

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

    # The rule's wake beats the routed digest, carries its note, and ends the wait.
    assert ridden.outcome == "woke"
    [wake] = _wakes("owner")
    assert "Reread the skill." in wake
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


def test_a_capped_rule_riding_a_routed_digest_counts_like_an_added_one(isolated_db):
    import asyncio

    _project(P)
    _agent("owner")
    _library_owner("owner")
    rule = asyncio.run(_tool("owner", operation="create", connector=library.LIBRARY_CONNECTOR,
                             event_type=library.ITEM_CHANGED_EVENT, note="Reread the skill."))

    [first] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-1"))
    [second] = deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed("m-2"))

    # The cap turns the second wake back into routing's digest, with the rule's count and path.
    assert (first.outcome, second.outcome) == ("woke", DIGESTED)
    [note] = _notes("owner")
    assert f"; 1 since rule {rule['id']} last woke you" in note["summary"]
    assert note["summary"].endswith("; rule note: Reread the skill.")
    assert json.loads(note["details_json"])["path"] == f"rule:{rule['id']}"


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
def _statements():
    """Every statement any pooled connection runs inside the block."""
    import infra.database as database

    seen: list[str] = []
    original = database._make_connection

    def _traced(path):
        conn = original(path)
        conn.set_trace_callback(seen.append)
        return conn

    with database._pool_lock:
        existing = list(database._all_connections)
    for conn in existing:
        conn.set_trace_callback(seen.append)
    database._make_connection = _traced
    try:
        yield seen
    finally:
        database._make_connection = original
        with database._pool_lock:
            current = list(database._all_connections)
        for conn in current:
            conn.set_trace_callback(None)


def _reads(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]


def test_platform_reads_per_event(isolated_db, monkeypatch):
    import asyncio

    _project(P)
    _agent("owner")
    _agent("watcher")
    _library_owner("owner")

    def run(manifest: str) -> list[str]:
        with _statements() as seen:
            deliver_platform_event_sync(library.LIBRARY_CONNECTOR, _library_changed(manifest))
        return _reads(seen)

    with patch("events.platform.rules.evaluate", lambda *_args: None):
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
        with _statements() as seen:
            await route(SYNC, _sync(7, n))
        return _reads(seen)

    with patch("events.connectors.github_rules.apply_routed_rules", lambda *a, **k: None):
        await run(0)
        before = await run(1)
    no_rule = await run(2)
    await _tool("watcher", operation="create", connector="github", event_type="pr.synchronize",
                match=[{"field": "repo", "op": "eq", "value": "x/y"}])
    unmatched = await run(3)
    await _tool("watcher", operation="create", connector="github", event_type="pr.synchronize")
    matched = await run(4)

    assert len(no_rule) - len(before) == 1
    assert len([s for s in no_rule if "event_rules" in s]) == 1
    assert len(unmatched) - len(before) == 1
    print(f"webhook reads: dev={len(before)} no_rule=+{len(no_rule) - len(before)} "
          f"unmatched=+{len(unmatched) - len(before)} matched=+{len(matched) - len(before)}")
