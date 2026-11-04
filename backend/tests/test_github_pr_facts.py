from __future__ import annotations

import json

import pytest

from events import rule_reads
from events.connectors import github_content_facts as content
from events.connectors import github_pr_facts as facts
from events.connectors import github_repo_activity as poller
from events.connectors import github_rules
from events.connectors.github import MANIFEST
from events.connectors.github_notifications_feed import GhResponse
from events.connectors.github_pr_quiet_reasons import mergeable_label
from events.envelope import normalize
from infra.database import get_db
from reviewer import gh_helpers


def pull(**over):
    return {"number": 7, "draft": False, "mergeable": None, "labels": [{"name": "b"}, {"name": "a"}],
            "head": {"sha": "h1", "repo": {"full_name": "fork/r"}},
            "base": {"ref": "dev", "sha": "b1", "repo": {"full_name": "o/r"}},
            "user": {"login": "someone"}, **over}


def webhook(action="synchronize", **over):
    return {"action": action, "repository": {"full_name": "o/r"}, "pull_request": pull(**over)}


def sent(action="synchronize", event="pr.synchronize", **over):
    """The facts a webhook carries before any read."""
    return normalize("github", webhook(action, **over), event).facts


def run(name, status="completed", conclusion="success", started="2026-01-01T00:00:00Z", id=1, completed=None):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started, "id": id,
            "completed_at": completed}


def page(*runs, total=None):
    return {"total_count": len(runs) if total is None else total, "check_runs": list(runs)}


def suite(conclusion="success", runs=1, status="completed", id=1):
    return {"id": id, "status": status, "conclusion": conclusion, "latest_check_runs_count": runs}


HELD = suite("action_required", runs=0, id=9)


def suites(*items):
    return {"total_count": len(items), "check_suites": list(items)}


@pytest.mark.parametrize("value, state", [(True, "MERGEABLE"), (False, "CONFLICTING"), (None, "UNKNOWN"),
                                          ("clean", "UNKNOWN"), (1, "UNKNOWN")])
def test_mergeable_is_a_string_enum_never_a_boolean(value, state):
    assert mergeable_label(value) == state


def test_pull_facts_come_from_the_pull_request_object():
    got = facts.pull_facts(pull())
    assert got == {"base_sha": "b1", "draft": False, "labels": ["a", "b"]}


@pytest.mark.parametrize("found, runs, state", [
    ([], [run("a"), run("b", conclusion="skipped"), run("c", conclusion="neutral")], "green"),
    ([], [run("a"), run("b", status="in_progress", conclusion=None)], "pending"),
    ([], [], "pending"),
    ([], [run("a", conclusion="failure"), run("b", status="queued", conclusion=None)], "failing"),
    ([], [run("a", conclusion="action_required")], "action_required"),
    ([HELD], [], "action_required"),  # a held suite has no runs; never pending
    ([HELD, suite()], [run("a")], "action_required"),
    ([HELD], [run("a", conclusion="failure")], "action_required"),
    # One suite held, another timed out: the maintainer's approval comes first.
    ([HELD, suite("timed_out", id=2)], [run("a", conclusion="timed_out")], "action_required"),
    ([suite("timed_out")], [run("a")], "failing"),
    # The merge gate holds on a cancelled run, so it is failing, not pending or green.
    ([suite("cancelled")], [run("a", conclusion="cancelled")], "failing"),
    # A zero-run suite that is not held never counts, whatever GitHub left it as.
    ([suite("stale", runs=0), suite(None, runs=0, status="queued", id=2)], [run("a")], "green"),
    ([], [run("a", conclusion="failure", started="2026-01-01T00:00:00Z", id=1),
          run("a", conclusion="success", started="2026-01-02T00:00:00Z", id=2)], "green"),
    # A re-run that started after the old run completed is the newest, even before it completes.
    ([], [run("a", conclusion="failure", completed="2026-01-01T00:05:00Z", id=1),
          run("a", status="in_progress", conclusion=None, started="2026-01-01T00:06:00Z", id=2)], "pending"),
    # Only the conclusions the merge gate counts green are green; a missing or unknown one fails.
    ([], [run("a"), run("b", conclusion=None)], "failing"),
    ([], [run("a"), run("b", conclusion="something_new")], "failing"),
])
def test_check_state_has_one_precedence_over_suites_and_newest_runs(found, runs, state):
    assert facts.check_state(found, runs) == state


@pytest.mark.parametrize("conclusions, ranked, state", [
    (["success", "skipped", "neutral"], "success", "green"),
    (["timed_out", "action_required", "failure"], "action_required", "action_required"),
    (["failure", "timed_out"], "timed_out", "failing"),
    (["success", "cancelled"], "cancelled", "failing"),
    (["cancelled", "something_new"], "failure", "failing"),  # unknown is never success
    (["success", None], "failure", "failing"),
    ([], "success", "green"),
])
def test_one_ranking_gives_both_conclusion_and_check_state(conclusions, ranked, state):
    assert facts.rank(conclusions) == ranked and facts.state_of(ranked) == state


def test_check_state_selects_the_run_the_reviewer_gate_selects():
    older = run("a", conclusion="failure", started="2026-01-01T00:00:00Z", completed="2026-01-01T00:09:00Z", id=1)
    newer = run("a", conclusion="success", started="2026-01-01T00:05:00Z", completed="2026-01-01T00:07:00Z", id=2)
    assert gh_helpers.newest_check_runs([older, newer]) == [older]  # completed_at decides, not started_at
    assert facts.check_state([], [older, newer]) == "failing"


def test_a_webhook_event_carries_the_payload_facts_and_keeps_its_own_head_as_sha():
    got = sent()
    assert got["sha"] == "h1" and got["branch"] == "dev"
    assert got["base_sha"] == "b1" and got["draft"] is False and got["labels"] == ["a", "b"]
    # Read only when a rule names one; mergeable too, since the payload's is almost always null.
    assert not set(facts.READ_FACTS) & set(got)
    assert "mergeable" not in sent(mergeable=True)
    assert "labels" in sent("closed", "pr.closed", merged=True)


def test_the_read_facts_are_declared_on_the_open_pr_and_checks_events_not_the_terminal_ones():
    for event in ("pr.opened", "pr.synchronize", "pr.ready_for_review", "pr.labeled", "pr.unlabeled"):
        assert set(facts.READ_FACTS) <= set(MANIFEST.decl(event).filterable_fields), event
    assert set(facts.READ_FACTS) <= set(MANIFEST.decl("head.checks_completed").filterable_fields)
    for event in ("pr.merged", "pr.closed"):
        assert not set(facts.READ_FACTS) & set(MANIFEST.decl(event).filterable_fields), event
        assert set(facts.PAYLOAD_FACTS) <= set(MANIFEST.decl(event).filterable_fields), event
    head = MANIFEST.decl("head.checks_completed")
    assert set(facts.HEAD_FACTS) <= set(head.filterable_fields) and {"conclusion", "head_sha"} <= set(head.filterable_fields)
    assert head.identity_facts == ("repo", "head_sha")
    assert MANIFEST.decl("pr.synchronize").identity_facts == ("repo", "pr_number", "action", "sha")


class Reads:
    def __init__(self, replies):
        self.replies, self.paths = replies, []

    async def __call__(self, _runner, _repo, path):
        self.paths.append(path)
        reply = next((v for k, v in self.replies.items() if k in path), None)
        return (GhResponse(200, {}, reply) if reply is not None else None), None


@pytest.fixture
def reads(monkeypatch):
    def install(replies):
        reader = Reads(replies)
        monkeypatch.setattr(poller, "gated_read", reader)
        return reader
    return install


CHECKS = "check-runs?per_page=100&page="
SUITES = "check-suites"


@pytest.mark.asyncio
async def test_an_open_pr_event_reads_the_pull_request_and_its_own_heads_checks_once(reads):
    reader = reads({"/pulls/7": pull(mergeable=True, head={"sha": "h2", "repo": {"full_name": "fork/r"}}),
                    "/check-suites": suites(suite()), "/check-runs": page(run("ci"))})
    got = await facts.head_facts(None, "o/r", 7, sent())
    assert reader.paths == ["repos/o/r/pulls/7", f"repos/o/r/commits/h1/{SUITES}", f"repos/o/r/commits/h1/{CHECKS}1"]
    # sha stays the payload's head and check_state describes it; the PR has moved on to h2.
    assert got["sha"] == "h1" and got["current_head"] == "h2" and got["check_state"] == "green"
    # mergeable comes only from the read; the payload owns draft and labels.
    assert got["mergeable"] == "MERGEABLE" and got["draft"] is False and got["labels"] == ["a", "b"]


def hundred(prefix):
    return [run(f"{prefix}{n}") for n in range(100)]


@pytest.mark.asyncio
async def test_check_runs_are_read_page_by_page_until_the_total(reads):
    pages = {f"{CHECKS}{n}": page(*hundred(f"p{n}-"), total=701) for n in range(1, 8)}
    pages[f"{CHECKS}8"] = page(run("late", conclusion="failure"), total=701)
    reader = reads({"/pulls/7": pull(), "/check-suites": suites(suite()), **pages})
    got = await facts.head_facts(None, "o/r", 7, sent())
    assert reader.paths[2:] == [f"repos/o/r/commits/h1/{CHECKS}{n}" for n in range(1, 9)]  # no page cap
    assert got["check_state"] == "failing"


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [None, page(total=101), {"check_runs": [run("x")]}])
async def test_a_failed_empty_or_uncounted_page_before_the_total_is_unreadable(reads, second):
    replies = {"/pulls/7": pull(), "/check-suites": suites(suite()), f"{CHECKS}1": page(*hundred("c"), total=101)}
    if second is not None:
        replies[f"{CHECKS}2"] = second
    reader = reads(replies)
    got = await facts.head_facts(None, "o/r", 7, sent())
    assert got["check_state"] == "unreadable" and len(reader.paths) == 4


@pytest.mark.asyncio
async def test_a_failed_suites_read_is_unreadable(reads):
    reader = reads({"/pulls/7": pull(), "/check-runs": page(run("ci"))})  # the runs alone would read green
    got = await facts.head_facts(None, "o/r", 7, sent())
    assert got["check_state"] == "unreadable" and reader.paths[-1] == f"repos/o/r/commits/h1/{SUITES}"


@pytest.mark.asyncio
async def test_a_spent_read_budget_ends_the_paging_as_unreadable(monkeypatch):
    left = [4]  # the PR read, the suites and two pages, then the budget is spent

    async def budgeted(_runner, _repo, path):
        if not left[0]:
            return None, "budget_exhausted"
        left[0] -= 1
        body = pull() if "/pulls/" in path else suites(suite()) if SUITES in path else page(*hundred("c"), total=10_000)
        return GhResponse(200, {}, body), None

    monkeypatch.setattr(poller, "gated_read", budgeted)
    got = await facts.head_facts(None, "o/r", 7, sent())
    assert got["check_state"] == "unreadable" and left == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pull_request", "refused", "budget_spent", "bad_page", "raises"])
async def test_an_attempted_read_always_sets_check_state(monkeypatch, failure):
    """Whatever fails once a read is attempted, ``check_state`` is there: ``unreadable``.

    Only ``current_head`` may be left out, and only when the PR read itself failed."""
    left = [2 if failure == "budget_spent" else 99]

    async def read(_runner, _repo, path):
        if failure == "raises":
            raise OSError("no gh")
        if failure == "refused":
            return None, "read_refused"
        if "/pulls/" in path:
            return (None, None) if failure == "pull_request" else (GhResponse(200, {}, pull()), None)
        if not left[0]:
            return None, "budget_exhausted"
        left[0] -= 1
        if SUITES in path:
            return GhResponse(200, {}, suites(suite())), None
        bad = {"check_runs": "not a list"} if failure == "bad_page" else page(*hundred("c"), total=10_000)
        return GhResponse(200, {}, bad), None

    monkeypatch.setattr(poller, "gated_read", read)
    for kind, got in (
        ("open", await facts.head_facts(None, "o/r", 7, sent())),
        ("checks", await facts.head_facts(None, "o/r", 7, {"repo": "o/r", "head_sha": "h1", "conclusion": "success"})),
    ):
        pull_failed = failure in ("pull_request", "refused", "raises")
        # A settled head reads no checks: only a failed PR read leaves its state unreadable.
        assert got["check_state"] == ("green" if kind == "checks" and not pull_failed else "unreadable"), kind
        if not pull_failed:
            assert got["current_head"] == "h1", kind


@pytest.mark.asyncio
@pytest.mark.parametrize("own, origin", [(True, "inside"), (False, "outside")])
async def test_a_head_checks_event_learns_whose_pull_request_it_is(reads, monkeypatch, own, origin):
    monkeypatch.setattr(content, "is_own", lambda login: own and login == "someone")
    reads({"/pulls/7": pull()})
    got = await facts.head_facts(None, "o/r", 7, {"repo": "o/r", "head_sha": "h1", "conclusion": "success"})
    assert got["author_origin"] == origin


@pytest.mark.asyncio
async def test_a_head_checks_event_keeps_its_settled_head_when_the_pull_request_moved(reads):
    reader = reads({"/pulls/7": pull(mergeable=True, head={"sha": "Y"}),
                    "/commits/X/check-runs": page(run("ci", conclusion="failure")),
                    "/commits/Y/check-runs": page(run("ci"))})
    settled = {"repo": "o/r", "head_sha": "X", "conclusion": "failure"}
    got = await facts.head_facts(None, "o/r", 7, settled)
    assert got["head_sha"] == "X" and got["current_head"] == "Y"
    assert got["check_state"] == "failing"  # about X: state_of its conclusion
    # The read refreshes only draft, mergeable and labels; the base stays the payload's.
    assert got["mergeable"] == "MERGEABLE" and got["labels"] == ["a", "b"] and "base_sha" not in got
    # The head's checks were read by the aggregator, so only the PR is read here.
    assert reader.paths == ["repos/o/r/pulls/7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [[HELD], [HELD, suite("timed_out", id=2)]])
@pytest.mark.parametrize("kind", ["open_pr", "head_checks"])
async def test_a_held_head_reads_action_required_on_every_event_kind(reads, found, kind):
    timed_out = [run("ci", conclusion="timed_out")] if len(found) > 1 else []
    reads({"/pulls/7": pull(), "/check-suites": suites(*found), "/check-runs": page(*timed_out)})
    if kind == "open_pr":
        got = await facts.head_facts(None, "o/r", 7, sent())
    else:
        settled = {"repo": "o/r", "head_sha": "h1", "conclusion": facts.rank(s["conclusion"] for s in found)}
        got = await facts.head_facts(None, "o/r", 7, settled)
        assert got["conclusion"] == "action_required"
    assert got["check_state"] == "action_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [{"repo": "o/r", "head_sha": "X"},
                                   {"head_sha": "X", "mergeable": "MERGEABLE"}])
async def test_a_failed_pull_request_read_keeps_the_head_and_has_no_current_head(reads, given):
    reader = reads({"/check-suites": suites(suite()), "/check-runs": page(run("ci"))})  # would answer green
    got = await facts.head_facts(None, "o/r", 7, given)
    assert got == {**given, "check_state": "unreadable"}  # no fact the read would have supplied
    assert reader.paths == ["repos/o/r/pulls/7"]


@pytest.mark.asyncio
async def test_a_pr_event_keeps_its_payload_facts_when_the_read_sees_others(reads):
    reads({"/pulls/7": pull(mergeable=False, draft=True, head={"sha": "h2"}), "/check-suites": suites(suite()),
           "/check-runs": page(run("ci", conclusion="failure"))})
    got = await facts.head_facts(None, "o/r", 7, sent(mergeable=True, labels=[{"name": "x"}]))
    assert got["sha"] == "h1" and got["current_head"] == "h2" and got["check_state"] == "failing"
    assert got["draft"] is False and got["labels"] == ["x"]  # the payload's
    assert got["mergeable"] == "CONFLICTING"  # only ever the read's


@pytest.mark.asyncio
async def test_failed_reads_leave_unknown_and_unreadable_never_raise(monkeypatch):
    async def broken(*_args):
        raise OSError("no gh")

    monkeypatch.setattr(poller, "gated_read", broken)
    got = await facts.head_facts(None, "o/r", 7, sent())
    assert got["check_state"] == "unreadable" and got["sha"] == "h1" and got["draft"] is False  # payload's
    assert "current_head" not in got and "mergeable" not in got


@pytest.mark.asyncio
async def test_enrichment_only_completes_open_pr_events(reads):
    reader = reads({"/pulls/7": pull(), "/check-suites": suites(suite()), "/check-runs": page(run("ci"))})
    opened = await facts.enriched(normalize("github", webhook("opened", mergeable=True), "pr.opened"), object())
    assert opened.facts["check_state"] == "green"
    closed = await facts.enriched(
        normalize("github", webhook("closed", merged=True), "pr.merged"), object())
    assert "check_state" not in closed.facts
    assert await facts.enriched(None) is None
    assert len(reader.paths) == 3  # the opened event's PR, suites and runs reads; the merged one reads nothing


def _rule(event_type, match, enabled=1, id="r1"):
    db = get_db()
    try:
        db.execute(
            'INSERT INTO event_rules (id,created_at,updated_at,enabled,scope,connector,event_type,"match",action,"to",'
            "mode,origin,created_by,authority_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, "t", "t", enabled, "project:p", "github", event_type, json.dumps(match), "deliver", "thread:w",
             "wake", "agent:w", "w", "self"))
        db.commit()
    finally:
        db.close()


REPO = {"field": "repo", "op": "eq", "value": "o/r"}
CHECK = {"field": "check_state", "op": "eq", "value": "green"}
HEAD = {"field": "current_head", "op": "present"}


AUTHOR = {"field": "author_origin", "op": "eq", "value": "outside"}


@pytest.mark.parametrize("match, enabled, event, asked", [
    ([REPO, CHECK], 1, "pr.synchronize", True),
    ([HEAD], 1, "pr.synchronize", True),
    ([REPO, AUTHOR], 1, "pr.synchronize", False),  # a payload fact on a PR event
    ([REPO, {"field": "mergeable", "op": "eq", "value": "MERGEABLE"}], 1, "pr.synchronize", True),
    ([REPO, AUTHOR], 1, "head.checks_completed", True),  # supplied only by the read there
    ([REPO, {"field": "draft", "op": "eq", "value": False}], 1, "head.checks_completed", True),
    ([REPO, {"field": "draft", "op": "eq", "value": False}], 1, "pr.synchronize", False),
    ([REPO, CHECK], 0, "pr.synchronize", False),  # disabled
    ([REPO, CHECK], 1, "pr.opened", False),  # another type's rule
    ([REPO, CHECK], 1, "pr.merged", False),  # a terminal type is never read
])
def test_a_rule_naming_a_fact_the_read_supplies_is_what_asks_for_the_read(isolated_db, match, enabled, event, asked):
    _rule(event if event == "pr.merged" or event == "head.checks_completed" else "pr.synchronize", match, enabled)
    assert rule_reads.type_rules("github", event, facts.read_facts(event))[2] is asked


def test_a_type_with_no_rule_asks_for_no_read(isolated_db):
    assert rule_reads.type_rules("github", "pr.synchronize", facts.READ_FACTS) == (False, False, False)


def test_the_read_facts_per_type_are_what_the_read_supplies_there():
    assert facts.read_facts("pr.opened") == facts.READ_FACTS
    assert set(facts.read_facts("head.checks_completed")) == {
        "current_head", "check_state", "draft", "mergeable", "labels", "author_origin"}
    assert facts.read_facts("pr.merged") == facts.read_facts("pr.closed") == ()


@pytest.mark.parametrize("match, reads_head", [([REPO, CHECK], True), ([REPO], False)])
def test_rule_scope_says_whether_a_rule_asks_for_the_read(isolated_db, match, reads_head):
    _rule("pr.synchronize", match)
    scope = github_rules.rule_scope("webhook.github.pr_synchronize", webhook(), None)
    assert scope.any_rule and scope.reads_head is reads_head


@pytest.mark.asyncio
@pytest.mark.parametrize("reads_head", [True, False])
async def test_route_preparation_completes_the_envelope_only_when_a_rule_asked(monkeypatch, reads_head):
    from events.connectors import github_route_preparation as preparation

    envelope = normalize("github", webhook("synchronize"), "pr.synchronize")
    seen, called = {}, []

    async def enriched(env):
        called.append(env)
        return type(env)(**{**env.__dict__, "facts": {**env.facts, "check_state": "green"}})

    def prepare(_unit, _event, _payload, _identity, _pr, scope, _delivery):
        seen["scope"] = scope

    monkeypatch.setattr(github_rules, "rule_scope",
                        lambda *_a: github_rules.RuleScope(envelope=envelope, reads_head=reads_head))
    monkeypatch.setattr(facts, "enriched", enriched)
    monkeypatch.setattr(preparation, "_prepare_route", prepare)
    await preparation.prepare_route_ordered("webhook.github.pr_synchronize", webhook(), None, None)
    assert bool(called) is reads_head
    assert ("check_state" in seen["scope"].envelope.facts) is reads_head
