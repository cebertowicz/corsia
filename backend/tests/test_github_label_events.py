"""Label and issue-lifecycle events: one normalization for the webhook copy and the poller copy.

Fixtures in ``fixtures/github_activity`` are GitHub's own ``issues/events`` rows and issue
list, and real ``pull_request.closed`` deliveries, recorded once from the repo and trimmed to
the fields the code reads. No ``labeled`` webhook delivery existed in the hook's three-day
retention (``issues`` is not subscribed and labels are rarely set on pull requests), so the
labeled webhook copies below are constructed from GitHub's documented payload shape.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from events import intake
from events.connectors import github_issue_activity as activity
from events.connectors import github_label_events as events
from events.connectors.github import MANIFEST
from events.connectors.github_notifications_feed import GhResponse
from events.connectors.github_outcomes import map_github_outcomes
from events.envelope import normalize
from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS, filter_attributes

REPO = "cebertowicz/corsia"
FIXTURES = Path(__file__).parent / "fixtures" / "github_activity"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def pr_webhook(action: str = "labeled", label: str = "connectors", labels=("connectors",), at: str | None = None) -> dict:
    """The labeled webhook for the recorded PR 1421, built to GitHub's documented shape."""
    row = fixture("row_pr_labeled")
    return {
        "action": action, "number": 1421, "repository": {"full_name": REPO},
        "label": {"name": label, "color": "0E8A16"}, "sender": {"login": row["actor"]["login"]},
        "pull_request": {"number": 1421, "labels": [{"name": name} for name in labels],
                         "head": {"sha": "9f2d6c1"}, "created_at": row["issue"]["created_at"],
                         "updated_at": at or row["created_at"]},
    }


def poller_copy(row: dict, heads: dict | None = None) -> tuple[str, dict, str]:
    delivery = activity.row_delivery(REPO, row, heads or {"1421": "9f2d6c1"})
    assert delivery is not None
    return delivery


def test_pr_labeled_webhook_and_poller_copies_match():
    webhook = events.change_event("pull_request", pr_webhook())
    event, payload, key = poller_copy(fixture("row_pr_labeled"))
    poller = events.change_event(event, payload)
    assert webhook and poller and event == "pull_request"
    assert (poller.type, poller.facts, poller.event_id) == (webhook.type, webhook.facts, webhook.event_id)
    assert webhook.type == "pr.labeled" and key == "labeled:cebertowicz/corsia:34114590651"
    assert webhook.facts == {
        "repo": REPO, "pr_number": 1421, "label": "connectors", "labels": ["connectors"],
        "actor": "cebertowicz", "at": "2026-10-24T06:25:11Z", "sha": "9f2d6c1",
    }


def test_issue_labeled_copies_match_and_carry_minutes_not_sha():
    row = fixture("row_issue_labeled")
    issue = row["issue"]
    webhook = events.change_event("issues", {
        "action": "labeled", "repository": {"full_name": REPO}, "label": row["label"],
        "sender": {"login": row["actor"]["login"]},
        "issue": {**issue, "updated_at": row["created_at"]},
    })
    event, payload, _ = poller_copy(row)
    poller = events.change_event(event, payload)
    assert event == "issues" and webhook and poller and poller == webhook
    assert webhook.type == "issue.labeled" and "sha" not in webhook.facts
    assert webhook.facts["minutes_since_opened"] >= 0 and webhook.facts["issue_number"] == issue["number"]
    assert set(webhook.facts) == {"repo", "issue_number", "label", "labels", "actor", "at", "minutes_since_opened"}


def test_unlabeled_carries_label_and_actor_with_its_own_type_and_id():
    row = {**fixture("row_pr_labeled"), "event": "unlabeled"}
    event, payload, _ = poller_copy(row)
    unlabeled = events.change_event(event, payload)
    labeled = events.change_event("pull_request", pr_webhook())
    assert unlabeled and labeled and unlabeled.type == "pr.unlabeled"
    assert (unlabeled.facts["label"], unlabeled.facts["actor"]) == ("connectors", "cebertowicz")
    assert unlabeled.event_id != labeled.event_id
    webhook = events.change_event("pull_request", pr_webhook("unlabeled"))
    assert webhook == unlabeled


def test_two_labels_on_one_head_are_two_events_and_a_re_add_is_a_third():
    first = events.change_event("pull_request", pr_webhook(label="p1", labels=("p1",)))
    second = events.change_event("pull_request", pr_webhook(label="cause:regression", labels=("p1", "cause:regression")))
    again = events.change_event("pull_request", pr_webhook(label="p1", at="2026-10-24T07:00:00Z"))
    assert first and second and again
    assert len({first.event_id, second.event_id, again.event_id}) == 3
    assert second.facts["labels"] == ["cause:regression", "p1"]


def test_issue_closed_reopened_and_opened_facts():
    closed_row = fixture("row_issue_closed")
    event, payload, _ = poller_copy(closed_row)
    closed = events.change_event(event, payload)
    assert closed and closed.type == "issue.closed"
    assert closed.facts["state_reason"] == "completed" and closed.facts["at"] == closed_row["created_at"]
    reopened = events.change_event(*poller_copy({**closed_row, "event": "reopened", "created_at": "2026-10-26T00:00:00Z"})[:2])
    assert reopened and reopened.type == "issue.reopened" and "state_reason" not in reopened.facts
    item = fixture("issues_list_item")
    opened = events.change_event("issues", {"action": "opened", "repository": {"full_name": REPO},
                                            "issue": item, "sender": {"login": item["user"]["login"]}})
    assert opened and opened.type == "issue.opened" and "minutes_since_opened" not in opened.facts
    assert opened.facts["labels"] == ["bug", "infrastructure", "p3"] and opened.facts["at"] == item["created_at"]
    # An issue opens once, so its identity needs no time.
    assert opened.event_id == events.change_event("issues", {
        "action": "opened", "repository": {"full_name": REPO}, "issue": {**item, "created_at": item["created_at"]},
    }).event_id


def test_closed_at_is_the_event_time_on_recorded_pairs_but_updated_at_can_drift_a_second():
    """A closed identity uses ``closed_at``, equal to the row time on 3 of 3 pairs; ``updated_at`` was a second late on 1 of 3."""
    pairs = fixture("closed_delivery_vs_rows")
    assert len(pairs) == 3
    drift = 0
    for pair in pairs:
        pull = pair["webhook"]["pull_request"]
        assert pair["rows"] and {row["created_at"] for row in pair["rows"]} == {pull["closed_at"]}
        drift += pull["updated_at"] != pull["closed_at"]
    assert drift == 1


def test_a_pull_request_close_or_a_row_without_time_is_not_built_here():
    assert activity.row_delivery(REPO, {**fixture("row_pr_labeled"), "event": "closed"}, {}) is None
    assert activity.row_delivery(REPO, {**fixture("row_pr_labeled"), "created_at": None}, {}) is None
    assert activity.row_delivery(REPO, {**fixture("row_issue_labeled"), "event": "assigned"}, {}) is None
    assert events.change_event("pull_request", {"action": "opened"}) is None
    assert events.change_event("issues", {"action": "labeled", "repository": {"full_name": REPO}, "issue": {}}) is None
    assert events.change_event("check_suite", {}) is None


def test_manifest_declares_label_as_a_filterable_field_and_the_per_change_identity():
    for suffix in ("pr.labeled", "pr.unlabeled", "issue.labeled", "issue.unlabeled"):
        decl = MANIFEST.decl(suffix)
        assert "label" in decl.filterable_fields and "actor" in decl.filterable_fields
        assert "label" in decl.identity_facts and "at" in decl.identity_facts
    assert "sha" in MANIFEST.decl("pr.labeled").filterable_fields
    assert "sha" not in MANIFEST.decl("issue.labeled").filterable_fields
    assert MANIFEST.decl("pr.labeled").routed_event == "webhook.github.pr_labeled"
    assert MANIFEST.classify(pr_webhook("unlabeled")) == "pr.unlabeled"
    assert MANIFEST.classify(pr_webhook()) == "pr.labeled"
    assert MANIFEST.classify({"action": "closed", "issue": {"number": 1}, "repository": {}}) == "issue.closed"


def test_the_legacy_envelope_and_the_intake_id_are_the_same_id():
    payload = pr_webhook()
    envelope = normalize("github", payload, "pr.labeled")
    change = events.change_event("pull_request", payload)
    assert change and envelope.event_id == f"github:pr.labeled:{change.event_id.split(':')[-1]}"
    assert {"label", "labels", "actor", "at"} <= set(envelope.facts)
    # A label change is a new identity from the review-label identity of before: label and time.
    assert "action" not in MANIFEST.decl("pr.labeled").identity_facts


def test_outcome_drafts_match_between_copies_and_survive_the_allowlist():
    webhook = map_github_outcomes("pull_request", pr_webhook())
    event, payload, _ = poller_copy(fixture("row_pr_labeled"))
    assert webhook == map_github_outcomes(event, payload) and len(webhook) == 1
    draft = webhook[0]
    assert draft.event_name == "github.pr.labeled" and draft.ts == "2026-10-24T06:25:11Z"
    kept, dropped = filter_attributes(draft.event_name, draft.attributes)
    assert dropped == [] and kept["label"] == "connectors" and kept["actor"] == "cebertowicz"
    assert kept["head_sha"] == "9f2d6c1" and kept["label_count"] == 1
    again = map_github_outcomes("pull_request", pr_webhook(label="p1", labels=("connectors", "p1")))[0]
    assert again.dedupe_key != draft.dedupe_key
    issue_row = fixture("row_issue_labeled")
    drafts = map_github_outcomes(*poller_copy(issue_row)[:2])
    assert [d.event_name for d in drafts] == ["github.issue.labeled"]
    kept, dropped = filter_attributes(drafts[0].event_name, drafts[0].attributes)
    assert dropped == [] and "minutes_since_opened" in kept and "head_sha" not in kept
    reopened = map_github_outcomes(*poller_copy({**issue_row, "event": "reopened"})[:2])
    assert [d.event_name for d in reopened] == ["github.issue.reopened"]
    assert {"github.pr.unlabeled", "github.issue.unlabeled"} <= set(ATTRIBUTE_ALLOWLISTS)


def test_the_two_copies_are_admitted_once_and_the_second_is_a_duplicate(isolated_db):
    change = events.change_event("pull_request", pr_webhook())
    event, payload, _ = poller_copy(fixture("row_pr_labeled"))
    copy = events.change_event(event, payload)
    assert change and copy

    def admit(item):
        return asyncio.run(intake.admit_event(
            event_type=f"github.{item.type}", facts=item.facts, origin="connector",
            producer="github", event_id=item.event_id))

    first, second = admit(change), admit(copy)
    assert first.accepted and not first.duplicate and first.refusal is None
    assert second.accepted and second.duplicate


def test_new_issues_seed_then_emit_only_issues_and_drop_pull_requests():
    state: dict = {}
    admitted: list = []

    async def admit(event, payload, key):
        admitted.append((event, payload["action"], key))

    item = fixture("issues_list_item")
    older = {**item, "number": 1400, "created_at": "2026-10-01T00:00:00Z"}
    pull = {**item, "number": 1401, "pull_request": {"url": "x"}, "created_at": "2026-10-27T00:00:00Z"}
    answers = [GhResponse(200, {}, [item, older]), GhResponse(304, {}, None),
               GhResponse(200, {}, [{**item, "number": 1463, "created_at": "2026-10-26T18:00:00Z"}, pull, item, older])]

    async def get(path, saved, now):
        return answers.pop(0)

    now = datetime.now(UTC)
    assert asyncio.run(activity.new_issues(get, admit, REPO, state, now)) is False
    assert state["issues_opened_at"] == item["created_at"] and admitted == []
    assert asyncio.run(activity.new_issues(get, admit, REPO, state, now)) is False
    assert asyncio.run(activity.new_issues(get, admit, REPO, state, now)) is True
    assert admitted == [("issues", "opened", f"opened:{REPO}#1463")]


def _admit(item, at=None):
    facts = {**item.facts, **({"at": at} if at else {})}
    event_id = item.event_id if at is None else events.compute_event_id(
        events.CONNECTOR, item.type, facts, events.IDENTITY[item.type])
    return asyncio.run(events.admit_change(
        intake.admit_event, event_type=f"github.{item.type}", facts=facts, origin="connector",
        producer="github", event_id=event_id))


def _copy_at(seconds: int):
    """The poller copy of PR 1421's label, its time ``seconds`` after the webhook's."""
    row = fixture("row_pr_labeled")
    at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) + timedelta(seconds=seconds)
    event, payload, _ = poller_copy({**row, "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return events.change_event(event, payload)


def test_a_copy_one_second_apart_is_a_duplicate_in_either_order(isolated_db):
    webhook = events.change_event("pull_request", pr_webhook())
    late, early = _copy_at(1), _copy_at(-4)
    assert webhook and late and early and late.event_id != webhook.event_id
    first = _admit(webhook)
    assert first.accepted and not first.duplicate
    assert _admit(late).duplicate and _admit(early).duplicate and _admit(webhook).duplicate


def test_the_reverse_order_poller_first_then_webhook_is_a_duplicate(isolated_db):
    webhook = events.change_event("pull_request", pr_webhook())
    assert not _admit(_copy_at(1)).duplicate
    assert _admit(webhook).duplicate


def test_copies_six_seconds_apart_or_a_re_add_minutes_apart_are_two_events(isolated_db):
    webhook = events.change_event("pull_request", pr_webhook())
    assert not _admit(webhook).duplicate
    assert not _admit(_copy_at(6)).duplicate
    assert not _admit(_copy_at(180)).duplicate


def test_opened_and_closed_stay_exact_only(isolated_db):
    assert "issue.opened" not in events.WINDOWED and "issue.closed" not in events.WINDOWED
    row = fixture("row_issue_closed")
    event, payload, _ = poller_copy(row)
    closed = events.change_event(event, payload)
    assert closed and closed.type == "issue.closed" and not _admit(closed).duplicate
    other = events.compute_event_id(events.CONNECTOR, "issue.closed", {**closed.facts, "at": "2026-10-26T17:56:07Z"},
                                    events.IDENTITY["issue.closed"])
    assert other != closed.event_id
