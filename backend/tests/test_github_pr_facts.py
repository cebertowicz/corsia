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


@pytest.mark.parametrize("value, state", [(True, "MERGEABLE"), (False, "CONFLICTING"), (None, "UNKNOWN"),
                                          ("clean", "UNKNOWN"), (1, "UNKNOWN")])
def test_mergeable_is_a_string_enum_never_a_boolean(value, state):
    assert mergeable_label(value) == state


def test_pull_facts_come_from_the_pull_request_object():
    got = facts.pull_facts(pull())
    assert got == {"base_sha": "b1", "draft": False, "labels": ["a", "b"]}


def test_a_webhook_event_carries_the_payload_facts_and_keeps_its_own_head_as_sha():
    got = sent()
    assert got["sha"] == "h1" and got["branch"] == "dev"
    assert got["base_sha"] == "b1" and got["draft"] is False and got["labels"] == ["a", "b"]
    assert "labels" in sent("closed", "pr.closed", merged=True)


def test_pr_events_declare_the_payload_and_head_facts_as_filterable():
    for event in ("pr.merged", "pr.closed"):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("own, origin", [(True, "inside"), (False, "outside")])
async def test_a_head_checks_event_learns_whose_pull_request_it_is(reads, monkeypatch, own, origin):
    monkeypatch.setattr(content, "is_own", lambda login: own and login == "someone")
    reads({"/pulls/7": pull()})
    got = await facts.head_facts(None, "o/r", 7, {"repo": "o/r", "head_sha": "h1", "conclusion": "success"})
    assert got["author_origin"] == origin


@pytest.mark.asyncio
async def test_a_head_checks_event_keeps_its_settled_head_and_reads_only_the_pull_request(reads):
    reader = reads({"/pulls/7": pull(mergeable=True, head={"sha": "Y"})})
    got = await facts.head_facts(None, "o/r", 7, {"repo": "o/r", "head_sha": "X", "conclusion": "failure"})
    assert got["head_sha"] == "X" and got["conclusion"] == "failure"
    assert got["labels"] == ["a", "b"] and got["base_sha"] == "b1" and got["draft"] is False
    assert reader.paths == ["repos/o/r/pulls/7"]


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [{"repo": "o/r", "head_sha": "X"}, {"head_sha": "X", "draft": True}])
async def test_a_failed_pull_request_read_leaves_the_facts_as_given(reads, given):
    reader = reads({})
    got = await facts.head_facts(None, "o/r", 7, given)
    assert got == given
    assert reader.paths == ["repos/o/r/pulls/7"]


@pytest.mark.asyncio
async def test_failed_reads_never_raise(monkeypatch):
    async def broken(*_args):
        raise OSError("no gh")

    monkeypatch.setattr(poller, "gated_read", broken)
    got = await facts.head_facts(None, "o/r", 7, {"sha": "h1", "draft": False})
    assert got == {"sha": "h1", "draft": False}


@pytest.mark.asyncio
async def test_enrichment_completes_reply_events_only(reads):
    reader = reads({"/pulls/7": pull()})
    opened = await facts.enriched(normalize("github", webhook("opened", mergeable=True), "pr.opened"), object())
    assert opened.facts == sent("opened", "pr.opened", mergeable=True)
    closed = await facts.enriched(
        normalize("github", webhook("closed", merged=True), "pr.merged"), object())
    assert await facts.enriched(None) is None
    assert reader.paths == []  # neither PR lifecycle event reads anything


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
DRAFT = {"field": "draft", "op": "eq", "value": False}
AUTHOR = {"field": "author_origin", "op": "eq", "value": "outside"}


@pytest.mark.parametrize("match, enabled, event, asked", [
    ([REPO, AUTHOR], 1, "pr.synchronize", False),  # a PR event is never read
    ([REPO, DRAFT], 1, "pr.synchronize", False),
    ([REPO, AUTHOR], 1, "head.checks_completed", True),  # supplied only by the read there
    ([REPO, DRAFT], 1, "head.checks_completed", True),
    ([REPO, {"field": "base_sha", "op": "present"}], 1, "head.checks_completed", True),
    ([REPO, AUTHOR], 0, "head.checks_completed", False),  # disabled
    ([REPO], 1, "head.checks_completed", False),  # names nothing the read supplies
])
def test_a_rule_naming_a_fact_the_read_supplies_is_what_asks_for_the_read(isolated_db, match, enabled, event, asked):
    _rule(event, match, enabled)
    assert rule_reads.type_rules("github", event, facts.read_facts(event))[2] is asked


@pytest.mark.parametrize("event, field, expected", [
    ("pr_comment.created", "author", False),
    ("pr_comment.created", "draft", True),
    ("review.submitted", "draft", True),
])
def test_reply_rule_gate(isolated_db, event, field, expected):
    _rule(event, [{"field": field, "op": "present"}])
    assert rule_reads.type_rules("github", event, facts.read_facts(event))[2] is expected


def test_the_read_facts_per_type_are_what_the_read_supplies_there():
    assert facts.read_facts("pr.opened") == facts.read_facts("pr.synchronize") == ()
    assert set(facts.read_facts("head.checks_completed")) == {"base_sha", "draft", "labels", "author_origin"}
    assert set(facts.read_facts("pr_comment.created")) == {"base_sha", "draft", "labels"}
    assert facts.read_facts("pr.merged") == facts.read_facts("pr.closed") == ()


@pytest.mark.parametrize("match, reads_head", [([REPO, DRAFT], True), ([REPO], False)])
def test_rule_scope_says_whether_a_rule_asks_for_the_read(isolated_db, match, reads_head):
    _rule("pr_comment.created", match)
    payload = {"action": "created", "repository": {"full_name": "o/r"},
               "issue": {"number": 7, "pull_request": {"url": "x"}},
               "comment": {"id": 5, "body": "hi", "user": {"login": "someone", "type": "User"}}}
    scope = github_rules.rule_scope("webhook.github.pr_comment", payload, None)
    assert scope.any_rule and scope.reads_head is reads_head


@pytest.mark.asyncio
@pytest.mark.parametrize("reads_head", [True, False])
async def test_route_preparation_completes_the_envelope_only_when_a_rule_asked(monkeypatch, reads_head):
    from events.connectors import github_route_preparation as preparation

    envelope = normalize("github", webhook("synchronize"), "pr.synchronize")
    seen, called = {}, []

    async def enriched(env):
        called.append(env)
        return type(env)(**{**env.__dict__, "facts": {**env.facts, "read": True}})

    def prepare(_unit, _event, _payload, _identity, _pr, scope, _delivery):
        seen["scope"] = scope

    monkeypatch.setattr(github_rules, "rule_scope",
                        lambda *_a: github_rules.RuleScope(envelope=envelope, reads_head=reads_head))
    monkeypatch.setattr(facts, "enriched", enriched)
    monkeypatch.setattr(preparation, "_prepare_route", prepare)
    await preparation.prepare_route_ordered("webhook.github.pr_synchronize", webhook(), None, None)
    assert bool(called) is reads_head
    assert ("read" in seen["scope"].envelope.facts) is reads_head
