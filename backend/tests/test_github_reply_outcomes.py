"""Reviews and comments become outcome events through real admission.

Fixtures are GitHub's published ``pull_request_review``, ``pull_request_review_comment`` and
``issue_comment`` webhook examples, trimmed to the fields the mappers read (bodies and the
diff hunk stay, so the tests can show they are never kept). The example comment is on a plain
issue, so the pull request comment is built from it as GitHub documents one: the same payload
with a ``pull_request`` member on ``issue``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import events.connectors.github_webhook as webhook_module
from events.connectors import github_outcome_input as shape
from events.connectors.github_admission import admit_github_delivery_async
from events.connectors.github_outcomes import map_github_outcomes
from events.markers import REVIEW_FP_MARKER_PREFIX, REVIEW_RESOLVED_MARKER, REVIEW_SUMMARY_MARKER
from infra import drain
from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS, filter_attributes

FIXTURES = Path(__file__).parent / "fixtures/github_outcomes"
REPO = "codertocat/hello-world"
HEAD = "ec26c3e57ca3a959ca5aad62de7213c562f8c821"
NAMES = (
    "pr_review_submitted", "pr_review_dismissed", "pr_review_comment_created", "issue_comment_created",
)


@pytest.fixture(autouse=True)
def _unrouted_scheduler(monkeypatch):
    """No type routes anywhere, so the scheduler leg is a no-op that settles the delivery."""
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    monkeypatch.setattr(webhook_module, "schedule_github_webhook", lambda *a, **k: None)


def delivery(name: str) -> tuple[str, dict[str, Any]]:
    recorded = json.loads((FIXTURES / f"{name}.json").read_text())
    return recorded["event"], copy.deepcopy(recorded["payload"])


def pr_comment(body: str | None = None, **issue: Any) -> dict[str, Any]:
    """The published issue comment, made a pull request's: ``issue.pull_request`` is present."""
    _, payload = delivery("issue_comment_created")
    payload["issue"].update(pull_request={"url": "https://api.github.com/repos/o/r/pulls/1"}, **issue)
    if body is not None:
        payload["comment"]["body"] = body
    return payload


def events(db) -> list[tuple[str, str, dict[str, Any]]]:
    rows = db.execute("SELECT event_name, dedupe_key, attributes FROM outcome_events ORDER BY dedupe_key")
    return [(name, key, json.loads(attributes)) for name, key, attributes in rows]


def counters(db) -> dict[tuple[str, str], int]:
    return {(r[0], r[1]): r[2] for r in db.execute("SELECT counter, event_name, n FROM outcome_event_counters")}


async def admit(event_type: str, payload: dict[str, Any], delivery_id: str):
    return await admit_github_delivery_async(event_type, payload, delivery_id)


async def test_a_submitted_review_is_one_event_with_its_facts(main_db):
    event_type, payload = delivery("pr_review_submitted")

    outcome = await admit(event_type, payload, "d-review")

    assert outcome.content["status"] == "ignored", "no routed event: the fact is the whole effect"
    ((name, key, attrs),) = events(main_db)
    assert (name, key) == ("github.pr.review.submitted", f"github:pr_review:{REPO}:237895671:submitted")
    assert attrs == {
        "repo": REPO, "pr_number": "2", "review_id": "237895671", "state": "commented",
        "commit_id": HEAD, "head_sha": HEAD, "author_login": "Codertocat", "author_association": "OWNER",
        "pr_author_login": "Codertocat", "has_reviewer_marker": "no", "minutes_since_pr_created": 0,
    }
    assert counters(main_db) == {}, "every fact passed the allowlist"
    (draft,) = map_github_outcomes(event_type, payload)
    assert draft.ts == "2019-05-15T15:20:38Z"
    assert draft.subject_key == f"github:pr:{REPO}#2"


async def test_a_dismissed_review_has_the_same_facts_and_the_dismissal_time(main_db):
    event_type, payload = delivery("pr_review_dismissed")
    payload["pull_request"]["updated_at"] = "2019-05-15T17:22:00Z"

    await admit(event_type, payload, "d-dismissed")

    ((name, key, attrs),) = events(main_db)
    assert (name, key) == ("github.pr.review.dismissed", f"github:pr_review:{REPO}:237895671:dismissed")
    assert attrs["state"] == "dismissed" and attrs["head_sha"] == HEAD and attrs["commit_id"] == HEAD
    assert attrs["minutes_since_pr_created"] == 121, "from the PR's creation to the dismissal, not the review"
    assert map_github_outcomes(event_type, payload)[0].ts == "2019-05-15T17:22:00Z"


async def test_a_submission_and_a_dismissal_of_one_review_are_two_events(main_db):
    _, submitted = delivery("pr_review_submitted")
    _, dismissed = delivery("pr_review_dismissed")

    await admit("pull_request_review", submitted, "d-1")
    await admit("pull_request_review", dismissed, "d-2")

    assert [name for name, _, _ in events(main_db)] == [
        "github.pr.review.dismissed", "github.pr.review.submitted",
    ]


async def test_a_review_comment_is_one_event_with_its_path_and_commit(main_db):
    event_type, payload = delivery("pr_review_comment_created")

    await admit(event_type, payload, "d-line")

    ((name, key, attrs),) = events(main_db)
    assert (name, key) == (
        "github.pr.review_comment.created", f"github:pr_review_comment:{REPO}:284312630:created",
    )
    assert attrs == {
        "repo": REPO, "pr_number": "2", "comment_id": "284312630", "path": "README.md",
        "commit_id": HEAD, "head_sha": HEAD, "author_login": "Codertocat", "author_association": "OWNER",
        "pr_author_login": "Codertocat", "has_reviewer_marker": "no", "minutes_since_pr_created": 0,
    }
    assert counters(main_db) == {}
    assert map_github_outcomes(event_type, payload)[0].subject_key == f"github:pr:{REPO}#2"


async def test_a_reply_in_a_thread_names_the_comment_it_answers(main_db):
    event_type, payload = delivery("pr_review_comment_created")
    payload["comment"]["in_reply_to_id"] = 284312000

    await admit(event_type, payload, "d-reply")

    ((_, _, attrs),) = events(main_db)
    assert attrs["in_reply_to_id"] == "284312000"


async def test_a_conversation_comment_on_a_pull_request_is_a_pr_comment_with_no_head(main_db):
    payload = pr_comment()

    await admit("issue_comment", payload, "d-pr-comment")

    ((name, key, attrs),) = events(main_db)
    assert (name, key) == ("github.pr.comment.created", f"github:pr_comment:{REPO}:492700400:created")
    assert attrs == {
        "repo": REPO, "pr_number": "1", "comment_id": "492700400", "author_login": "Codertocat",
        "author_association": "OWNER", "pr_author_login": "Codertocat", "has_reviewer_marker": "no",
        "minutes_since_pr_created": 0,
    }, "issue_comment carries no head, and no read is made to find one"
    assert map_github_outcomes("issue_comment", payload)[0].subject_key == f"github:pr:{REPO}#1"


async def test_a_conversation_comment_on_a_plain_issue_is_an_issue_comment(main_db):
    event_type, payload = delivery("issue_comment_created")
    payload["issue"]["created_at"] = "2019-05-14T15:20:18Z"

    await admit(event_type, payload, "d-issue-comment")

    ((name, key, attrs),) = events(main_db)
    assert (name, key) == ("github.issue.comment.created", f"github:issue_comment:{REPO}:492700400:created")
    assert attrs == {
        "repo": REPO, "issue_number": "1", "comment_id": "492700400", "author_login": "Codertocat",
        "author_association": "OWNER", "issue_author_login": "Codertocat",
        "minutes_since_issue_created": 1440,
    }
    assert counters(main_db) == {}
    assert map_github_outcomes(event_type, payload)[0].subject_key == f"github:issue:{REPO}#1"


async def test_a_comment_seen_twice_is_one_event(main_db):
    """The delivery log already keys a comment by its id; the event key is the second guard."""
    _, payload = delivery("pr_review_comment_created")

    await admit("pull_request_review_comment", payload, "d-webhook")
    again = await admit("pull_request_review_comment", payload, "d-webhook")
    polled = await admit("pull_request_review_comment", copy.deepcopy(payload), "d-poller")

    assert again.content["status"] == "duplicate" and polled.content["status"] == "duplicate"
    assert len(events(main_db)) == 1


async def test_a_review_redelivered_under_a_new_delivery_id_is_one_event(main_db):
    _, payload = delivery("pr_review_submitted")

    await admit("pull_request_review", payload, "d-first")
    redelivered = await admit("pull_request_review", copy.deepcopy(payload), "d-second")

    assert redelivered.content["status"] == "ignored", "a new delivery of a recorded review is admitted"
    assert len(events(main_db)) == 1
    assert counters(main_db) == {("duplicate_ignored", "github.pr.review.submitted"): 1}


async def test_one_id_in_each_id_space_is_three_events(main_db):
    """Reviews, line comments and conversation comments are separate GitHub id spaces; a
    conversation comment is one id space whether its parent is an issue or a pull request."""
    _, review = delivery("pr_review_submitted")
    _, line = delivery("pr_review_comment_created")
    _, conversation = delivery("issue_comment_created")
    review["review"]["id"] = line["comment"]["id"] = conversation["comment"]["id"] = 777

    await admit("pull_request_review", review, "d-a")
    await admit("pull_request_review_comment", line, "d-b")
    await admit("issue_comment", conversation, "d-c")

    assert sorted(key for _, key, _ in events(main_db)) == [
        f"github:issue_comment:{REPO}:777:created", f"github:pr_review:{REPO}:777:submitted",
        f"github:pr_review_comment:{REPO}:777:created",
    ]


@pytest.mark.parametrize("event_type,fixture,action", [
    ("pull_request_review", "pr_review_submitted", "edited"),
    ("pull_request_review_comment", "pr_review_comment_created", "edited"),
    ("pull_request_review_comment", "pr_review_comment_created", "deleted"),
    ("issue_comment", "issue_comment_created", "edited"),
    ("issue_comment", "issue_comment_created", "deleted"),
])
async def test_an_edit_or_a_delete_writes_nothing(main_db, event_type, fixture, action):
    _, payload = delivery(fixture)
    payload["action"] = action

    outcome = await admit(event_type, payload, "d-other")

    assert outcome.content["status"] == "ignored"
    assert events(main_db) == [] and counters(main_db) == {}
    assert map_github_outcomes(event_type, payload) == []


@pytest.mark.parametrize("marker", [
    REVIEW_SUMMARY_MARKER, f"{REVIEW_FP_MARKER_PREFIX} 0123456789abcdef -->", REVIEW_RESOLVED_MARKER,
])
async def test_a_published_reviewer_marker_is_detected_in_each_body_whoever_sent_it(main_db, marker):
    """The sender is the published human-looking login: the marker alone decides."""
    body = f"Finding text.\n\n{marker}"
    _, review = delivery("pr_review_submitted")
    review["review"].update(body=body, id=11)
    _, line = delivery("pr_review_comment_created")
    line["comment"].update(body=body, id=12)
    comment = pr_comment(body)
    comment["comment"]["id"] = 13

    await admit("pull_request_review", review, "d-m1")
    await admit("pull_request_review_comment", line, "d-m2")
    await admit("issue_comment", comment, "d-m3")

    flags = [attrs["has_reviewer_marker"] for _, _, attrs in events(main_db)]
    assert flags == ["yes", "yes", "yes"]
    assert all(attrs["author_login"] == "Codertocat" for _, _, attrs in events(main_db))


async def test_a_quoted_or_absent_marker_is_not_a_reviewer_marker(main_db):
    _, review = delivery("pr_review_submitted")
    review["review"].update(body=f"> {REVIEW_SUMMARY_MARKER}", id=21)
    _, other = delivery("pr_review_submitted")
    other["review"].update(body=None, id=22)

    await admit("pull_request_review", review, "d-q1")
    await admit("pull_request_review", other, "d-q2")

    assert [attrs["has_reviewer_marker"] for _, _, attrs in events(main_db)] == ["no", "no"]


async def test_no_body_title_or_diff_text_reaches_a_row(main_db):
    _, review = delivery("pr_review_submitted")
    review["review"]["body"] = "a review body that must not be kept"
    _, line = delivery("pr_review_comment_created")
    _, issue = delivery("issue_comment_created")

    await admit("pull_request_review", review, "d-b1")
    await admit("pull_request_review_comment", line, "d-b2")
    await admit("issue_comment", issue, "d-b3")

    stored = json.dumps(events(main_db))
    for text in ("must not be kept", "more emoji", "@@ -1 +1 @@", "totally right"):
        assert text not in stored


@pytest.mark.parametrize("created,expected", [
    ("2019-05-15T13:15:33Z", 125),
    ("2019-05-15T15:20:38Z", 0),
    ("2019-05-15T15:21:33Z", None),
])
async def test_minutes_are_whole_from_creation_and_absent_when_the_end_precedes_it(main_db, created, expected):
    _, payload = delivery("pr_review_submitted")
    payload["pull_request"]["created_at"] = created

    await admit("pull_request_review", payload, "d-min")

    ((_, _, attrs),) = events(main_db)
    assert attrs.get("minutes_since_pr_created") == expected
    assert attrs["state"] == "commented", "the rest of the review is still recorded"


async def test_a_deleted_author_account_records_the_rest(main_db):
    _, payload = delivery("pr_review_comment_created")
    payload["comment"]["user"] = None
    del payload["comment"]["author_association"]

    await admit("pull_request_review_comment", payload, "d-ghost")

    ((_, _, attrs),) = events(main_db)
    assert "author_login" not in attrs and "author_association" not in attrs
    assert attrs["pr_author_login"] == "Codertocat" and attrs["path"] == "README.md"


@pytest.mark.parametrize("event_type,fixture,missing", [
    ("pull_request_review", "pr_review_submitted", ("review", "id")),
    ("pull_request_review", "pr_review_submitted", ("review", "submitted_at")),
    ("pull_request_review", "pr_review_submitted", ("pull_request", "number")),
    ("pull_request_review", "pr_review_dismissed", ("pull_request", "updated_at")),
    ("pull_request_review_comment", "pr_review_comment_created", ("comment", "id")),
    ("pull_request_review_comment", "pr_review_comment_created", ("comment", "created_at")),
    ("issue_comment", "issue_comment_created", ("comment", "id")),
    ("issue_comment", "issue_comment_created", ("issue", "number")),
])
async def test_a_payload_missing_a_consumed_field_is_refused_counted_and_recorded(
    main_db, event_type, fixture, missing,
):
    _, payload = delivery(fixture)
    del payload[missing[0]][missing[1]]
    with pytest.raises(shape.PayloadRefusal):
        map_github_outcomes(event_type, payload)

    await admit(event_type, payload, "d-refused")

    assert events(main_db) == []
    assert counters(main_db) == {("mapping_failed", event_type): 1}


def test_every_fact_the_mappers_emit_is_on_the_allowlist():
    deliveries = [delivery(name) for name in NAMES] + [("issue_comment", pr_comment())]
    seen = set()
    for event_type, payload in deliveries:
        for draft in map_github_outcomes(event_type, payload):
            kept, dropped = filter_attributes(draft.event_name, draft.attributes)
            assert dropped == [] and kept == draft.attributes
            seen.add(draft.event_name)
    assert seen == {
        "github.pr.review.submitted", "github.pr.review.dismissed", "github.pr.review_comment.created",
        "github.pr.comment.created", "github.issue.comment.created",
    }
    assert seen <= set(ATTRIBUTE_ALLOWLISTS)
