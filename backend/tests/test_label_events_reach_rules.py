"""Label changes reach event rules and, past the review label's own route, wake nobody.

Each test drives the real mapper, the webhook router and the ``event_rules`` tool on a
disposable database. Only the final hand-off to a live turn is replaced.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from events.github_mapper import map_github_event
from events.router import RouteOutcome
from infra.database import get_db
from reviewer.reviewer_categories import LABEL_REVIEW
from tests.event_routing_helpers import mk_session, register_pr, route
from tools.event_rules import EventRulesTool

REPO = "o/r"
P = "proj-a"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    monkeypatch.setattr("events.connectors.github_notifications_feed.projects_tracking_github_repository",
                        lambda repository: frozenset({P}) if repository == REPO else frozenset())
    monkeypatch.setattr("events.connectors.github_notifications_feed.tracked_github_repositories",
                        lambda project_id=None: frozenset({REPO}) if project_id == P else frozenset())


@pytest.fixture
def sent():
    calls: list[str] = []

    async def _send(thread_id, _row, _content, **_kwargs):
        calls.append(thread_id)

    with patch("events.delivery.send_thread_input", AsyncMock(side_effect=_send)):
        yield calls


def _rows(sql: str, *args) -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _agent(thread_id: str) -> str:
    db = get_db()
    try:
        db.execute("INSERT OR IGNORE INTO projects (id, name, created_at, updated_at) VALUES (?,?,?,?)",
                   (P, P, "2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z"))
        db.commit()
    finally:
        db.close()
    mk_session(thread_id, project_id=P)
    db = get_db()
    try:
        db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (thread_id,))
        db.commit()
    finally:
        db.close()
    return thread_id


async def _rule(thread_id: str, event_type: str) -> dict:
    out = await EventRulesTool().execute(
        _thread_id=thread_id, operation="create", connector="github", event_type=event_type,
        match=[{"field": "repo", "op": "eq", "value": REPO},
               {"field": "label", "op": "prefix", "value": "cause:"}],
        note="Cause label changed.",
    )
    assert not out.startswith(("Refused:", "Error:")), out
    return json.loads(out)


def _pr_label(action: str, name: str, *, number: int = 7, sender: dict | None = None) -> dict:
    return {
        "action": action, "label": {"name": name}, "repository": {"full_name": REPO},
        "sender": sender or {"login": "alice", "type": "User"},
        "pull_request": {
            "number": number, "title": "Fix", "user": {"login": "alice"},
            "updated_at": "2026-10-30T18:06:03Z", "labels": [{"name": name}],
            "head": {"sha": "abc123", "repo": {"full_name": REPO}},
            "base": {"ref": "dev", "repo": {"full_name": REPO}},
        },
    }


def _issue_label(action: str, name: str, *, number: int = 9) -> dict:
    return {
        "action": action, "label": {"name": name}, "repository": {"full_name": REPO},
        "sender": {"login": "alice", "type": "User"},
        "issue": {
            "number": number, "title": "Broken", "user": {"login": "alice"},
            "created_at": "2026-10-29T18:06:03Z", "updated_at": "2026-10-30T18:06:03Z",
            "labels": [{"name": name}],
        },
    }


def _fired(rule: dict) -> list[dict]:
    return _rows("SELECT * FROM event_route_decisions WHERE rule_id=?", rule["id"])


@pytest.mark.parametrize("event_type,payload,event,kind", [
    ("pull_request", _pr_label("labeled", "cause:environment"), "webhook.github.pr_labeled", "pr.labeled"),
    ("pull_request", _pr_label("unlabeled", "cause:environment"), "webhook.github.pr_unlabeled", "pr.unlabeled"),
    ("issues", _issue_label("labeled", "cause:environment"), "webhook.github.issue_labeled", "issue.labeled"),
    ("issues", _issue_label("unlabeled", "cause:environment"), "webhook.github.issue_unlabeled", "issue.unlabeled"),
])
@pytest.mark.asyncio
async def test_any_label_change_fires_a_rule_and_wakes_no_one_else(
    isolated_db, sent, event_type, payload, event, kind,
):
    _agent("owner")
    _agent("writer")
    register_pr(REPO, 7, "owner")
    rule = await _rule("writer", kind)

    assert map_github_event(event_type, payload) == event
    result = await route(event, payload)

    assert sent == []  # the PR's owner isn't woken by a label
    assert (result.outcome, result.reason) == (RouteOutcome.PARKED, "label_rule_only")
    assert [row["outcome"] for row in _fired(rule)] == ["woke"]
    assert _rows("SELECT id FROM dead_letter_events") == []


@pytest.mark.asyncio
async def test_a_label_from_the_agents_account_still_reaches_rules(isolated_db, sent):
    _agent("owner")
    _agent("writer")
    register_pr(REPO, 7, "owner")
    rule = await _rule("writer", "pr.labeled")
    payload = _pr_label("labeled", "cause:environment", sender={"login": "ci-app[bot]", "type": "Bot"})

    result = await route("webhook.github.pr_labeled", payload)

    assert result.reason == "label_rule_only" and sent == []
    assert [row["outcome"] for row in _fired(rule)] == ["woke"]


@pytest.mark.asyncio
async def test_the_owners_own_rule_on_its_pr_still_delivers(isolated_db, sent):
    _agent("owner")
    register_pr(REPO, 7, "owner")
    rule = await _rule("owner", "pr.labeled")

    await route("webhook.github.pr_labeled", _pr_label("labeled", "cause:environment"))

    assert [row["outcome"] for row in _fired(rule)] == ["woke"]


@pytest.mark.asyncio
async def test_a_person_adding_the_review_label_keeps_the_owner_wake(isolated_db, sent):
    _agent("owner")
    register_pr(REPO, 7, "owner")

    result = await route("webhook.github.pr_labeled", _pr_label("labeled", LABEL_REVIEW))

    assert result.outcome == RouteOutcome.ACCEPTED and sent == ["owner"]


@pytest.mark.asyncio
async def test_the_agents_account_adding_the_review_label_stays_unwoken(isolated_db, sent):
    _agent("owner")
    register_pr(REPO, 7, "owner")
    payload = _pr_label("labeled", LABEL_REVIEW, sender={"login": "ci-app[bot]", "type": "Bot"})

    await route("webhook.github.pr_labeled", payload)

    assert sent == []


@pytest.mark.asyncio
async def test_a_label_on_an_unowned_outside_pr_with_no_rule_dead_letters_nothing(isolated_db, sent):
    result = await route("webhook.github.pr_labeled", _pr_label("labeled", "bug", number=41))

    assert result.outcome == RouteOutcome.PARKED and result.reason == "label_rule_only"
    assert _rows("SELECT id FROM dead_letter_events") == []


def test_a_label_change_with_no_label_name_is_not_an_event():
    payload = {"action": "unlabeled", "label": None}
    assert map_github_event("pull_request", payload) is None
    assert map_github_event("issues", payload) is None
