"""Count the words an agent writes into a document's comment threads."""

import json

import pytest

import core.attention_notice as notice
from doceditor.writer_line import Queued

CHECK = (
    "Before the person reads it: does each line carry its whole idea without leaving them a "
    "question, and does it say whether it is how things are today, a plan, or already done? "
    "If not, rewrite it now."
)


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(notice, "emit_product_event", lambda *a, **k: recorded.append(k))

    def no_database():
        raise AssertionError("a comment notice reads nothing from the database")

    monkeypatch.setattr(notice, "get_db", no_database)
    return recorded


def _notice(tool_name, arguments, result):
    return notice.attention_notice(thread_id="agent", tool_name=tool_name, arguments=arguments, result=result)


def test_reply_in_a_document_thread_is_counted(events):
    arguments = {"artifact_id": "doc", "parent_id": "root", "body": "Fixed the **table** as you asked."}
    rendered = _notice("doc_comment_create", arguments, json.dumps({"comment": {"id": "c2"}}))
    assert rendered == (
        "[CORSIA attention notice] Your comment: 6 words, at least 1 minute to read.\n" + CHECK
    )
    assert len(events) == 1
    properties = events[0]["properties"]
    assert properties["handover"] == "comment"
    assert properties["comment_words"] == 6
    assert properties["measured"] is False
    assert properties["waiting_documents"] is None


def test_comment_saved_in_line_counts_once(events):
    queued = Queued(root_id="root", position=2, holder="Documents & Artifacts").text
    assert _notice("doc_comment_create", {"body": "Two words."}, queued) is not None
    # Posting the saved write unchanged sends no body, so it is not counted again.
    assert _notice("doc_comment_update", {"comment_id": "c2", "post_as_is": True}, json.dumps({"id": "c2"})) is None
    assert [event["properties"]["handover"] for event in events] == ["comment"]


def test_edited_body_counts_as_an_edit(events):
    rendered = _notice("doc_comment_update", {"comment_id": "c2", "body": "Three new words."},
                       json.dumps({"comment": {"id": "c2"}}))
    assert rendered.startswith("[CORSIA attention notice] Your comment: 3 words")
    assert events[0]["properties"]["handover"] == "comment_edit"
    assert events[0]["properties"]["comment_edit_words"] == 3


@pytest.mark.parametrize("result", [
    "Error: Empty comment body",
    "Error creating document comment: {'error': 'not_found'}",
    "Error: client_id collides with another artifact's comment; re-mint and retry",
    None,
])
def test_refused_comment_is_not_counted(events, result):
    assert _notice("doc_comment_create", {"body": "Not saved."}, result) is None
    assert events == []


@pytest.mark.parametrize("arguments", [
    {"comment_id": "c2", "resolved": True},
    {"comment_id": "c2", "acked": True},
    {"comment_id": "c2", "decision": "accept"},
    {"comment_id": "c2", "leave": True},
    {"comment_id": "c2", "body": "   "},
])
def test_update_without_new_words_is_not_counted(events, arguments):
    assert _notice("doc_comment_update", arguments, json.dumps({"comment": {"id": "c2"}})) is None
    assert events == []


def test_region_comment_with_an_image_result_is_counted(events):
    image_result = object()
    rendered = _notice("doc_comment_create", {"body": "This box is off.", "region": {}}, image_result)
    assert rendered.startswith("[CORSIA attention notice] Your comment: 4 words")
    assert events[0]["properties"]["comment_words"] == 4
