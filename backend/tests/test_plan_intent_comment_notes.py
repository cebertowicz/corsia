"""A person's chat comment notes are their words; the agent text a comment quotes is not."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from infra.db_fts import _init_messages_fts
from infra.thread_driver import admit_input
from infra.time import utc_now_iso
from plans import intent_words, person_input, service
from plans.boundary import mutate
from tests.plan_scene import ACTOR, db  # noqa: F401

NOTE = "Bug. This reply shows nothing; fix the comment reply."
AGENT_TEXT = "The agent wrote this summary line."


def _comment_only(db, message_id: str) -> None:
    """Send a comments-only reply (blank text) through real admission, which indexes its comments."""
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) VALUES (?,?,?,?,?,?)",
        (f"target-{message_id}", "root", "assistant", AGENT_TEXT, "t", 1),
    )
    db.commit()
    admit_input(
        "root", role="user", content="", intent="apply_now", message_id=message_id,
        turn_id=message_id, boot_epoch="test", admitted_at=utc_now_iso(),
        materialize_passive_digest=False, db=db, metadata={"reply_context": {"comments": [{
            "id": "key-1", "seq": 1, "from_off": 4, "to_off": 9, "quote": "agent wrote",
            "anchor_prefix": "", "anchor_suffix": "", "note": NOTE,
        }]}},
    )
    assert db.execute(
        "SELECT count(*) FROM chat_comments WHERE user_message_id=?", (message_id,),
    ).fetchone()[0] == 1


def _add(db, plan_id: str, mutation_id: str, words: list[dict] | None, *, turn_id: str = "agent-turn"):
    params = {"title": "Comment ask", "acceptance_criteria": "Asked in a comment", "size": "S",
              "intent": "Fix the comment reply"}
    if words is not None:
        params["intent_words"] = words
    return mutate(plan_id, "add", params, actor=replace(ACTOR, turn_id=turn_id), provenance="test",
                  mutation_id=mutation_id, db=db).result


def test_a_comment_note_is_quotable_as_authority_from_an_agent_turn(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    _comment_only(db, "comment-reply")

    result = _add(db, plan.id, "note-authority", [{"quote": "fix the comment reply.", "role": "authority"}])

    row = db.execute(
        "SELECT author,role,source_kind,source_id FROM plan_node_intent_words WHERE node_id=?",
        (result["node"]["id"],),
    ).fetchone()
    assert tuple(row) == ("person", "authority", "message", "comment-reply")


def test_the_quoted_agent_text_is_not_the_persons_words(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    _comment_only(db, "comment-reply")

    with pytest.raises(service.PlanInputError) as refusal:
        _add(db, plan.id, "quote-authority", [{"quote": "agent wrote", "role": "authority"}])

    assert str(refusal.value) == intent_words.QUOTE_NOT_FOUND


def test_a_comment_only_turn_is_a_person_origin_and_its_note_is_captured(db):
    plan = service.ensure_plan("thread", "root", db=db)
    _comment_only(db, "comment-turn")
    db.execute("UPDATE messages SET active_turn_id='comment-turn' WHERE id='comment-turn'")

    assert person_input.load(db, "message", "comment-turn").words == NOTE
    result = _add(db, plan.id, "comment-origin", None, turn_id="comment-turn")

    words = intent_words.newest(db, result["node"]["id"])
    assert [(word["author"], word["role"], word["quote"]) for word in words] == [("person", "origin", NOTE)]


def test_message_words_put_text_before_notes_and_skip_blank_notes():
    metadata = {"reply_context": {"comments": [{"note": "  first  "}, {"note": " "}, {"note": "second"}]}}

    assert intent_words.message_words(role="user", content=" Typed ", metadata=metadata) == "Typed\nfirst\nsecond"
    assert intent_words.message_words(role="user", content="", metadata=None) == ""
    assert intent_words.message_words(role="assistant", content="Typed", metadata=metadata) == ""
    relayed = {**metadata, "sender_kind": "agent"}
    assert intent_words.message_words(role="user", content="", metadata=json.dumps(relayed)) == ""
