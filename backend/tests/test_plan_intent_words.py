"""Intent words retain their provenance evidence under the detail-read cap."""
from __future__ import annotations

from dataclasses import replace

from infra.db_fts import _init_messages_fts
from infra.input_admission import accept_thread_input
from plans import intent_words, service
from plans.boundary import mutate
from tests.plan_scene import ACTOR, db  # noqa: F401


def test_detail_keeps_an_oldest_origin_word_among_five(db):
    plan = service.ensure_plan("thread", "root", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Intent", acceptance_criteria="Recorded", intent="Keep the request aligned", db=db,
    )
    intent_words.append(db, node.id, [{
        "quote": "The original request", "author": "person", "role": "origin",
        "source_kind": "message", "source_id": "origin-message",
    }], revision=1)
    for index in range(4):
        intent_words.append(db, node.id, [{
            "quote": f"Newer supporting word {index}", "author": "person", "role": "bears_on",
            "source_kind": "message", "source_id": f"message-{index}",
        }], revision=1)

    words = intent_words.newest(db, node.id)

    assert len(words) == 3
    assert {word["quote"] for word in words} >= {"The original request"}
    assert words[0]["role"] == "origin"


def test_add_captures_the_person_input_consumed_by_its_turn(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    accept_thread_input(
        "root", role="user", content="Preserve this request.", intent="apply_now",
        message_id="origin-message", db=db,
    )
    db.execute("UPDATE messages SET active_turn_id='turn-1' WHERE id='origin-message'")

    result = mutate(
        plan.id, "add", {
            "title": "Preserve", "acceptance_criteria": "Request remains", "size": "S",
            "intent": "Preserve this request.",
        },
        actor=replace(ACTOR, turn_id="turn-1"), provenance="test", mutation_id="intent-origin", db=db,
    ).result

    words = intent_words.newest(db, result["node"]["id"])
    assert [(word["author"], word["role"], word["quote"]) for word in words] == [
        ("person", "origin", "Preserve this request."),
    ]


def test_add_resolves_a_person_authority_quote_through_the_message_producer(db):
    # This compact plan fixture omits the normal startup FTS hook.
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    accept_thread_input(
        "root", role="user", content="Build the exact requested outcome.", intent="apply_now",
        message_id="authority-message", db=db,
    )

    result = mutate(
        plan.id, "add", {
            "title": "Build", "acceptance_criteria": "Outcome exists", "size": "S",
            "intent": "Build the exact requested outcome.",
            "intent_words": [{"quote": "Build the exact requested outcome.", "role": "authority"}],
        },
        actor=ACTOR, provenance="test", mutation_id="intent-authority", db=db,
    ).result

    detail = service.get_node(result["node"]["id"], db=db)
    assert detail.intent == "Build the exact requested outcome."
    assert intent_words.newest(db, detail.id)[0]["role"] == "authority"

    from plans.public import intent_of
    assert intent_of(detail.id, db=db) == {
        "node_id": detail.id, "title": "Build", "requirements_revision": 1,
        "intent": "Build the exact requested outcome.", "acceptance_criteria": "Outcome exists",
        "intent_words": intent_words.newest(db, detail.id),
    }
