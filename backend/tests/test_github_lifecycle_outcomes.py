"""Lifecycle facts from GitHub's published webhook payload examples.

Source: https://octokit.github.io/webhooks/payload-examples/api.github.com/index.json
The fixture keeps only consumed fields from the documented examples.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import events.connectors.github_webhook as webhook_module
from events.connectors.github_admission import admit_github_delivery_async
from infra import drain

FIXTURES = Path(__file__).parent / "fixtures/github_outcomes"
REPO = "codertocat/hello-world"
SHA = "6113728f27ae82c7b1a177c8d03f9e96e0adf246"


@pytest.fixture(autouse=True)
def _unrouted_scheduler(monkeypatch):
    monkeypatch.setattr(drain, "_turn_admissions", 0)
    monkeypatch.setattr(webhook_module, "schedule_github_webhook", lambda *a, **k: None)


def rows(db):
    return [(name, json.loads(attrs)) for name, attrs in db.execute(
        "SELECT event_name, attributes FROM outcome_events ORDER BY dedupe_key"
    )]


@pytest.mark.parametrize("index,name,subject,facts", [
    (0, "github.pr.ready", f"github:pr:{REPO}#2", {"repo": REPO, "pr": "2", "minutes_since_pr_created": 0}),
    (1, "github.pr.drafted", f"github:pr:{REPO}#2", {"repo": REPO, "pr": "2"}),
    (2, "github.issue.assigned", f"github:issue:{REPO}#1",
     {"repo": REPO, "issue": "1", "assignee_login": "Codertocat", "minutes_since_issue_created": 0}),
    # Same subject as the CI events for this SHA, so commit facts join.
    (3, "github.status", f"github:commit:{REPO}@{SHA}",
     {"repo": REPO, "sha": SHA, "context": "default", "state": "success"}),
    (4, "github.release.published", f"github:release:{REPO}:17372790",
     {"repo": REPO, "tag": "0.0.1", "prerelease": "no", "draft": "no"}),
])
async def test_lifecycle_facts_and_redelivery(main_db, index, name, subject, facts):
    fixture = json.loads((FIXTURES / "lifecycle.json").read_text())[index]
    payload = fixture["payload"]
    await admit_github_delivery_async(fixture["event"], payload, "delivery")
    await admit_github_delivery_async(fixture["event"], payload, "delivery")
    assert rows(main_db) == [(name, facts)]
    assert not ({"body", "title", "description", "release_notes"} & facts.keys())
    assert [s for (s,) in main_db.execute("SELECT subject_key FROM outcome_events")] == [subject]


async def test_drafted_does_not_read_unneeded_created_at(main_db):
    fixture = json.loads((FIXTURES / "lifecycle.json").read_text())[1]
    fixture["payload"]["pull_request"]["created_at"] = "not a timestamp"
    await admit_github_delivery_async("pull_request", fixture["payload"], "draft")
    assert rows(main_db)[0][0] == "github.pr.drafted"


async def test_status_context_with_spaces_is_retained(main_db):
    fixture = json.loads((FIXTURES / "lifecycle.json").read_text())[3]
    fixture["payload"]["context"] = "continuous-integration / build"
    await admit_github_delivery_async("status", fixture["payload"], "status-spaces")
    assert rows(main_db)[0][1]["context"] == "continuous-integration / build"


@pytest.mark.parametrize("fixture_name,event,parent,expected", [
    ("pr_review_submitted", "pull_request_review", "pull_request", "github.pr.review.submitted"),
    ("pr_review_dismissed", "pull_request_review", "pull_request", "github.pr.review.dismissed"),
    ("pr_review_comment_created", "pull_request_review_comment", "pull_request", "github.pr.review_comment.created"),
    ("issue_comment_created", "issue_comment", "pr_issue", "github.pr.comment.created"),
    ("issue_comment_created", "issue_comment", "issue", "github.issue.comment.created"),
])
async def test_opener_association(main_db, fixture_name, event, parent, expected):
    payload = copy.deepcopy(json.loads((FIXTURES / f"{fixture_name}.json").read_text())["payload"])
    parent_field = "issue" if parent == "pr_issue" else parent
    payload[parent_field]["author_association"] = "OWNER"
    if parent == "pr_issue":
        payload["issue"]["pull_request"] = {"url": "https://api.github.com/repos/o/r/pulls/1"}
    await admit_github_delivery_async(event, payload, "association")
    (name, attrs), = rows(main_db)
    assert name == expected
    field = "issue_author_association" if parent == "issue" else "pr_author_association"
    assert attrs[field] == "OWNER"
