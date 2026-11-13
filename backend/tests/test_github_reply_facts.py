"""Reply webhook facts and their PR read sources."""
import json
from dataclasses import asdict

import pytest

from events.connectors import github_pr_facts as pr_facts
from events.connectors import github_repo_activity as poller
from events.connectors.github import MANIFEST
from events.connectors.github_reply_facts import REPLY_FILTERABLE, REPLY_TYPES
from events.identity_formula import MalformedEventError
from events.connectors.github_notifications_feed import GhResponse
from events.envelope import normalize
from events.markers import REVIEW_SUMMARY_MARKER

PR = {"number": 7, "user": {"login": "pr-author"}, "head": {"sha": "old"},
      "base": {"sha": "payload-base"}, "draft": False, "labels": [{"name": "payload"}]}


def payload(kind, body="BODY_CANARY"):
    item = {"id": 11, "body": body, "user": {"login": "reply-author"}, "author_association": "CONTRIBUTOR"}
    raw = {"action": "submitted" if kind == "review" else "created", "repository": {"full_name": "o/r"}}
    if kind == "issue":
        raw.update(issue={"number": 7, "user": {"login": "pr-author"}, "pull_request": {"url": "x"}}, comment=item)
    elif kind == "line":
        raw.update(pull_request=PR, comment={**item, "path": "a.py", "commit_id": "c1", "in_reply_to_id": 5})
    else:
        raw.update(pull_request=PR, review={**item, "state": "CHANGES_REQUESTED", "commit_id": "c2"})
    return raw


def envelope(kind, body="BODY_CANARY"):
    return normalize("github", payload(kind, body), "review.submitted" if kind == "review" else "pr_comment.created")


@pytest.mark.parametrize("kind", ["issue", "line", "review"])
def test_reply_facts_and_no_body(kind):
    event = envelope(kind)
    facts = event.facts
    assert (facts["author"], facts["pr_author_login"], facts["pr_author_origin"], facts["pr_number"]) == ("reply-author", "pr-author", "outside", 7)
    assert facts["review_id" if kind == "review" else "comment_id"] == 11
    assert "BODY_CANARY" not in json.dumps(asdict(event))
    assert "sha" not in facts and "author_login" not in facts
    if kind == "line":
        assert (facts["path"], facts["commit_id"], facts["in_reply_to_id"]) == ("a.py", "c1", 5)
    if kind == "review":
        assert facts["state"] == "changes_requested"
    assert not {"base_sha", "draft", "labels"} & facts.keys()


@pytest.mark.parametrize("kind", ["issue", "line", "review"])
def test_reply_with_no_user_names_no_author(kind):
    raw = payload(kind)
    raw["review" if kind == "review" else "comment"]["user"] = None
    facts = normalize("github", raw, "review.submitted" if kind == "review" else "pr_comment.created").facts
    assert "author" not in facts and facts["author_origin"] == "outside"
    assert facts["pr_author_login"] == "pr-author"


@pytest.mark.parametrize("kind", ["line", "review"])
def test_edited_reply_with_null_user_never_inherits_pr_author(kind):
    raw = payload(kind)
    raw["review" if kind == "review" else "comment"]["user"] = None
    raw["action"] = "edited"
    raw["review" if kind == "review" else "comment"]["updated_at"] = "2026-11-04T18:00:00Z"
    facts = normalize("github", raw, "review.edited" if kind == "review" else "pr_comment.edited").facts
    assert "author" not in facts and facts["author_origin"] == "outside"


@pytest.mark.parametrize("field,value", [("id", True), ("path", 3), ("user", {"login": 3}),
                                        ("body", []), ("in_reply_to_id", "5"), ("state", False)])
def test_malformed_reply_is_refused_on_event_path(field, value):
    raw = payload("line")
    raw["comment"][field] = value
    with pytest.raises(MalformedEventError):
        normalize("github", raw, "pr_comment.created")


@pytest.mark.parametrize("body,expected", [("review: release ABCDEF1\nmore", "abcdef1"), ("x\nreview: release abcdef1", None), ("review: release abcdef", None), ("review: release abcdef1 extra", None)])
def test_release_first_line_only(body, expected):
    for kind in ("issue", "line"):
        assert envelope(kind, body).facts.get("release_sha") == expected
    assert "release_sha" not in envelope("review", body).facts


def test_marker_and_declarations():
    assert envelope("issue", REVIEW_SUMMARY_MARKER).facts["has_reviewer_marker"]
    for event in REPLY_TYPES:
        assert set((*REPLY_FILTERABLE, *pr_facts.PAYLOAD_FACTS)) <= set(MANIFEST.decl(event).filterable_fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["issue", "line", "review"])
async def test_read_uses_one_source_for_each_type(monkeypatch, kind):
    paths = []
    async def read(_runner, _repo, path):
        paths.append(path)
        body = {**PR, "head": {"sha": "live"}, "base": {"sha": "read-base"}, "draft": True,
                "labels": [{"name": "read"}], "user": {"login": "different"}, "mergeable": True}
        return GhResponse(200, {}, body), None
    monkeypatch.setattr(poller, "gated_read", read)
    got = (await pr_facts.enriched(envelope(kind), object())).facts
    assert len(paths) == 1 and "/pulls/" in paths[0]  # one PR read; no check reads
    assert not {"check_state", "current_head", "mergeable"} & set(got)
    assert (got["base_sha"], got["draft"], got["labels"]) == ("read-base", True, ["read"])
    assert got["pr_author_login"] == "pr-author"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["issue", "line", "review"])
async def test_failed_pull_read_leaves_the_reply_as_it_came(monkeypatch, kind):
    async def read(*_args):
        return None, None
    monkeypatch.setattr(poller, "gated_read", read)
    sent = envelope(kind)
    got = (await pr_facts.enriched(sent, object())).facts
    assert got == sent.facts
