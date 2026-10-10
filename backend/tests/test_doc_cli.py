
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doc_cli  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def _fake_request(method, base, path, token, body=None, query=None):
        calls.append({"method": method, "path": path, "body": body, "query": query})
        if path == "/api/artifacts/resolve":
            return {"found": True, "artifact": {"id": "art-1"}}
        if path.endswith("/comments") and method == "GET":
            return {"comments": []}
        return {"comment": {"id": "c1"}}

    monkeypatch.setattr(doc_cli, "_resolve_token", lambda x: "tok")
    monkeypatch.setattr(doc_cli, "_request", _fake_request)
    monkeypatch.setenv("CORSIA_THREAD_ID", "sess-self")
    return calls


def _last(calls):
    return calls[-1]



def test_comment_sends_doc_path_quote_body(captured):
    doc_cli.main(["comment", "notes.md", "hello world", "--quote", "the span"])
    c = _last(captured)
    assert c["method"] == "POST" and c["path"] == "/api/delegation/docs/comments"
    assert c["body"]["doc_path"] == os.path.abspath("notes.md")
    assert c["body"]["quote"] == "the span"
    assert c["body"]["body"] == "hello world"
    assert c["body"]["as_session_id"] == "sess-self"
    assert "near" not in c["body"]


def test_comment_near_passed_through(captured):
    doc_cli.main(["comment", "notes.md", "b", "--quote", "x", "--near", "hint"])
    assert _last(captured)["body"]["near"] == "hint"


def test_comment_with_by_id_targets_artifact(captured):
    doc_cli.main(["comment", "art-9", "body", "--quote", "x", "--by-id"])
    b = _last(captured)["body"]
    assert b["artifact_id"] == "art-9" and "doc_path" not in b


def test_top_level_token_before_subcommand(captured):
    doc_cli.main(["--token", "ovr", "resolve", "c-1", "--as", "s"])
    assert _last(captured)["path"] == "/api/delegation/docs/comments/c-1"


def test_review_sends_review_true(captured):
    doc_cli.main(["review", "notes.md", "looks good"])
    b = _last(captured)["body"]
    assert b["review"] is True and b["doc_path"] == os.path.abspath("notes.md")
    assert b["body"] == "looks good"


def test_reply_sends_parent_id(captured):
    doc_cli.main(["reply", "c-parent", "agreed"])
    b = _last(captured)["body"]
    assert b["parent_id"] == "c-parent" and b["body"] == "agreed"
    assert b["as_session_id"] == "sess-self"



def test_resolve_patches_comment(captured):
    doc_cli.main(["resolve", "c-7"])
    c = _last(captured)
    assert c["method"] == "PATCH"
    assert c["path"] == "/api/delegation/docs/comments/c-7"
    assert c["body"] == {"resolved": True, "as_session_id": "sess-self"}


def test_ack_patches_comment(captured):
    doc_cli.main(["ack", "c-7"])
    assert _last(captured)["body"] == {"acked": True, "as_session_id": "sess-self"}


def test_accept_all_from_author_patches_the_delegation_route(captured):
    doc_cli.main(["accept", "c-7", "--all-from-author"])
    call = _last(captured)
    assert call["method"] == "PATCH"
    assert call["path"] == "/api/delegation/docs/comments/c-7"
    assert call["body"] == {
        "decision": "accept",
        "all_from_author": True,
        "as_session_id": "sess-self",
    }


def test_self_id_explicit_overrides_env(captured):
    doc_cli.main(["resolve", "c-7", "--as", "sess-explicit"])
    assert _last(captured)["body"]["as_session_id"] == "sess-explicit"


def test_self_id_missing_fails_loud(monkeypatch):
    monkeypatch.setattr(doc_cli, "_resolve_token", lambda x: "tok")
    monkeypatch.setattr(doc_cli, "_request", lambda *a, **k: {})
    monkeypatch.delenv("CORSIA_THREAD_ID", raising=False)
    with pytest.raises(SystemExit) as e:
        doc_cli.main(["resolve", "c-7"])
    assert e.value.code == 2



def test_comments_resolves_path_then_lists(captured):
    doc_cli.main(["comments", "notes.md"])
    assert captured == [
        {
            "method": "GET",
            "path": "/api/artifacts/resolve",
            "body": None,
            "query": {"path": os.path.abspath("notes.md")},
        },
        {
            "method": "GET",
            "path": "/api/artifacts/art-1/comments",
            "body": None,
            "query": None,
        },
    ]


def test_comments_uses_one_path_lookup_for_code_artifact(monkeypatch):
    probed: list[dict] = []

    def _fake(method, base, path, token, body=None, query=None):
        if path == "/api/artifacts/resolve":
            probed.append(query)
            return {"artifact": {"id": "code-art"}}
        return {"comments": []}

    monkeypatch.setattr(doc_cli, "_resolve_token", lambda x: "tok")
    monkeypatch.setattr(doc_cli, "_request", _fake)
    rc = doc_cli.main(["comments", "script.py"])
    assert rc == 0
    assert probed == [{"path": os.path.abspath("script.py")}]


def test_submit_closes_document_feedback_now(captured):
    doc_cli.main(["submit", "notes.md"])
    call = _last(captured)
    assert call["path"] == "/api/delegation/docs/feedback/submit"
    assert call["body"] == {
        "doc_path": os.path.abspath("notes.md"),
        "as_session_id": "sess-self",
    }


def test_retry_accepts_artifact_id(captured):
    doc_cli.main(["retry", "art-9", "--by-id"])
    call = _last(captured)
    assert call["path"] == "/api/delegation/docs/feedback/retry"
    assert call["body"] == {"artifact_id": "art-9", "as_session_id": "sess-self"}


def test_request_review_is_distinct_and_carries_stable_key(captured):
    doc_cli.main(["request-review", "art-9", "--by-id", "--reviewer", "reviewer", "--note", "Check purpose", "--client-id", "stable-key"])
    call = _last(captured)
    assert call["method"] == "POST" and call["path"] == "/api/delegation/docs/reviews"
    assert call["body"] == {"artifact_id": "art-9", "reviewer": "reviewer", "note": "Check purpose", "client_id": "stable-key", "as_session_id": "sess-self"}


def test_request_review_generates_key_and_review_cancel_reuses_author(captured):
    doc_cli.main(["request-review", "notes.md", "--reviewer", "reviewer"])
    assert _last(captured)["body"]["client_id"]
    doc_cli.main(["review-cancel", "request-1"])
    call = _last(captured)
    assert call["method"] == "DELETE" and call["path"] == "/api/delegation/docs/reviews/request-1"
    assert call["query"] == {"as_session_id": "sess-self"}


def test_help_distinguishes_review_comment_from_request_review(capsys):
    with pytest.raises(SystemExit) as caught:
        doc_cli.main(["--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "Whole-doc review comment" in help_text
    assert "Queue a review task in another agent's plan" in help_text
