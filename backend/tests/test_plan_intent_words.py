"""Intent words retain their provenance evidence under the detail-read cap."""
from __future__ import annotations

from dataclasses import replace

import pytest

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


@pytest.mark.parametrize("operation", ("add", "insert"))
def test_add_and_insert_refuse_missing_intent(db, operation):
    plan = service.ensure_plan("thread", "root", db=db)

    with pytest.raises(service.PlanInputError) as refusal:
        mutate(
            plan.id, operation, {
                "title": "Ungrounded", "acceptance_criteria": "It is grounded", "size": "S",
            }, actor=ACTOR, provenance="test", mutation_id=f"intent-required-{operation}", db=db,
        )

    assert str(refusal.value) == "task creation requires intent"


@pytest.mark.parametrize("operation", ("add", "insert"))
def test_add_and_insert_require_authority_when_no_person_origin(db, operation):
    plan = service.ensure_plan("thread", "root", db=db)
    params = {
        "title": "Ungrounded", "acceptance_criteria": "It is grounded", "size": "S",
        "intent": "Ground the requested outcome",
    }

    with pytest.raises(service.PlanInputError) as refusal:
        mutate(
            plan.id, operation, params, actor=ACTOR, provenance="test",
            mutation_id=f"intent-authority-{operation}", db=db,
        )

    assert str(refusal.value) == (
        "task creation needs intent_words with an exact person quote and role: authority"
    )


def test_intent_and_quote_byte_limits_and_paraphrase_refusal(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    accept_thread_input(
        "root", role="user", content="Build the exact requested outcome.", intent="apply_now",
        message_id="quoted-person-input", db=db,
    )
    base = {
        "title": "Bounded", "acceptance_criteria": "The bounds are clear", "size": "S",
        "intent_words": [{"quote": "Build the exact requested outcome.", "role": "authority"}],
    }

    with pytest.raises(service.PlanInputError) as intent_refusal:
        mutate(
            plan.id, "add", {**base, "intent": "i" * 601}, actor=ACTOR,
            provenance="test", mutation_id="overlong-intent", db=db,
        )
    assert str(intent_refusal.value) == "intent exceeds the 600-byte limit"

    with pytest.raises(service.PlanInputError) as quote_refusal:
        mutate(
            plan.id, "add", {**base, "intent": "Within the limit", "intent_words": [
                {"quote": "q" * 401, "role": "authority"},
            ]}, actor=ACTOR, provenance="test", mutation_id="overlong-quote", db=db,
        )
    assert str(quote_refusal.value) == "quote exceeds the 400-byte limit"

    with pytest.raises(service.PlanInputError) as paraphrase_refusal:
        mutate(
            plan.id, "add", {**base, "intent": "Within the limit", "intent_words": [
                {"quote": "Build what was requested.", "role": "authority"},
            ]}, actor=ACTOR, provenance="test", mutation_id="paraphrased-quote", db=db,
        )
    assert str(paraphrase_refusal.value) == (
        "quote was not found; quote exactly, or pass it as your own words with author: agent"
    )


def test_doc_comment_quote_resolves_and_agent_words_skip_lookup(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES ('intent-doc','local','document','intent-doc','active','{}','t','t')"""
    )
    db.execute(
        """INSERT INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES ('intent-doc-registration','intent-doc','root','owner-active','t','t')"""
    )
    db.execute(
        """INSERT INTO doc_comments
           (id,artifact_id,author,author_kind,body,created_at,updated_at)
           VALUES ('intent-comment','intent-doc','Steve','user','Keep the release scope narrow.','t','t')"""
    )
    db.commit()

    result = mutate(
        plan.id, "add", {
            "title": "Narrow scope", "acceptance_criteria": "Scope remains narrow", "size": "S",
            "intent": "Keep the release scope narrow",
            "intent_words": [
                {"quote": "Keep the release scope narrow.", "role": "authority"},
                {"quote": "I will verify the narrow path.", "author": "agent", "role": "bears_on"},
            ],
        }, actor=ACTOR, provenance="test", mutation_id="comment-and-agent-words", db=db,
    ).result

    rows = db.execute(
        "SELECT author,source_kind,source_id,quote,role FROM plan_node_intent_words WHERE node_id=? ORDER BY rowid",
        (result["node"]["id"],),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("person", "doc_comment", "intent-comment", "Keep the release scope narrow.", "authority"),
        ("agent", "message", "root", "I will verify the narrow path.", "bears_on"),
    ]


def test_agent_only_turn_records_agent_origin_and_still_needs_person_authority(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    accept_thread_input(
        "root", role="user", content="Proceed with the approved work.", intent="apply_now",
        message_id="person-authority", db=db,
    )
    accept_thread_input(
        "root", role="user", content="Delegate this implementation.", intent="apply_now",
        message_id="agent-relay", metadata={"sender_kind": "agent"}, db=db,
    )
    db.execute("UPDATE messages SET active_turn_id='agent-turn' WHERE id='agent-relay'")

    with pytest.raises(service.PlanInputError) as refusal:
        mutate(
            plan.id, "add", {
                "title": "Unapproved delegated task", "acceptance_criteria": "It is approved", "size": "S",
                "intent": "Delegate the implementation",
            }, actor=replace(ACTOR, turn_id="agent-turn"), provenance="test",
            mutation_id="agent-turn-refusal", db=db,
        )
    assert str(refusal.value) == (
        "task creation needs intent_words with an exact person quote and role: authority"
    )

    result = mutate(
        plan.id, "add", {
            "title": "Approved delegated task", "acceptance_criteria": "It is approved", "size": "S",
            "intent": "Delegate the implementation",
            "intent_words": [{"quote": "Proceed with the approved work.", "role": "authority"}],
        }, actor=replace(ACTOR, turn_id="agent-turn"), provenance="test",
        mutation_id="agent-turn-authorized", db=db,
    ).result
    words = intent_words.newest(db, result["node"]["id"])
    assert ("agent", "origin", "Delegate this implementation.") in {
        (word["author"], word["role"], word["quote"]) for word in words
    }


def test_editing_intent_or_words_restates_but_description_does_not(db):
    _init_messages_fts(db)
    plan = service.ensure_plan("thread", "root", db=db)
    accept_thread_input(
        "root", role="user", content="Keep the original outcome.", intent="apply_now",
        message_id="revision-authority", db=db,
    )
    created = mutate(
        plan.id, "add", {
            "title": "Revision target", "acceptance_criteria": "Outcome is checked", "size": "S",
            "intent": "Keep the original outcome",
            "intent_words": [{"quote": "Keep the original outcome.", "role": "authority"}],
        }, actor=ACTOR, provenance="test", mutation_id="revision-create", db=db,
    ).result["node"]

    intent_edit = mutate(
        plan.id, "edit", {
            "node_id": created["id"], "expected_revision": created["revision"],
            "intent": "Keep the corrected outcome",
        }, actor=ACTOR, provenance="test", mutation_id="revision-intent", db=db,
    ).result["node"]
    words_edit = mutate(
        plan.id, "edit", {
            "node_id": created["id"], "expected_revision": intent_edit["revision"],
            "intent_words": [{"quote": "Agent verification note", "author": "agent", "role": "bears_on"}],
        }, actor=ACTOR, provenance="test", mutation_id="revision-words", db=db,
    ).result["node"]
    description_edit = mutate(
        plan.id, "edit", {
            "node_id": created["id"], "expected_revision": words_edit["revision"],
            "description": "Working notes are not requirements.",
        }, actor=ACTOR, provenance="test", mutation_id="revision-description", db=db,
    ).result["node"]

    assert (
        intent_edit["requirements_revision"],
        words_edit["requirements_revision"],
        description_edit["requirements_revision"],
    ) == (2, 3, 3)
    restatements = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='node_restated' ORDER BY rowid",
        (created["id"],),
    ).fetchall()
    assert len(restatements) == 2


def test_creation_paths_inherit_the_canonical_intent_and_exempt_the_gate(db):
    from plans import mutations

    root_plan = service.ensure_plan("thread", "root", db=db)
    canonical = service.add_node(
        root_plan.id, kind="task", title="Canonical", intent="Preserve the outcome",
        acceptance_criteria="The canonical outcome is proved", db=db,
    )
    source = service.add_node(
        root_plan.id, kind="task", title="Inherited source", intent_node_id=canonical.id,
        acceptance_criteria="The inherited source is proved", db=db,
    )

    split = mutate(
        root_plan.id, "split", {
            "node_id": source.id, "title": "Split task", "acceptance_criteria": "Split proof", "size": "S",
            "expected_revision": source.revision,
        }, actor=ACTOR, provenance="test", mutation_id="intent-split", db=db,
    ).result["node"]
    follow_up = mutate(
        root_plan.id, "add_follow_up", {
            "node_id": source.id, "title": "Follow-up", "acceptance_criteria": "Follow-up proof", "size": "S",
            "expected_revision": service.get_node(source.id, db=db).revision,
        }, actor=ACTOR, provenance="test", mutation_id="intent-follow-up", db=db,
    ).result["node"]

    service.transition_node(source.id, "active", db=db)
    mutations.attach_spawned_thread(source.id, "child", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    attached = mutate(
        child_plan.id, "add", {
            "title": "Attached worker step", "acceptance_criteria": "Worker proof", "size": "S",
        }, actor=replace(ACTOR, thread_id="child"), provenance="test",
        mutation_id="intent-attached-worker", db=db,
    ).result["node"]

    accept_source = service.add_node(
        root_plan.id, kind="task", title="Accept source", intent_node_id=canonical.id,
        acceptance_criteria="The accepted source is proved", db=db,
    )
    service.transition_node(accept_source.id, "active", db=db)
    reviewed = mutate(
        root_plan.id, "request_review", {
            "node_id": accept_source.id,
            "expected_revision": service.get_node(accept_source.id, db=db).revision,
            "evidence": "The source is ready for review.",
        }, actor=ACTOR, provenance="test", mutation_id="intent-review", db=db,
    ).result["node"]
    accepted_follow_up = mutate(
        root_plan.id, "accept_with_follow_up", {
            "node_id": accept_source.id, "expected_revision": reviewed["revision"],
            "title": "Accepted follow-up", "acceptance_criteria": "Accepted follow-up proof", "size": "S",
            "verification_result": "passed",
        }, actor=ACTOR, provenance="test", mutation_id="intent-accepted-follow-up", db=db,
    ).result["follow_up"]

    assert {
        service.get_node(node["id"], db=db).intent_node_id
        for node in (split, follow_up, attached, accepted_follow_up)
    } == {canonical.id}


def test_plan_read_detail_bounds_words_and_exposes_the_canonical_intent(db):
    from plans.current import read_current_status

    plan = service.ensure_plan("thread", "root", db=db)
    canonical = service.add_node(
        plan.id, kind="task", title="Canonical intent", intent="Keep the request aligned",
        acceptance_criteria="The request is satisfied", db=db,
    )
    intent_words.append(db, canonical.id, [{
        "quote": "The original request", "author": "person", "role": "origin",
        "source_kind": "message", "source_id": "origin",
    }], revision=1)
    for index in range(4):
        intent_words.append(db, canonical.id, [{
            "quote": (f"Recent supporting word {index} " + "x" * 260),
            "author": "agent", "role": "bears_on", "source_kind": "message", "source_id": f"agent-{index}",
        }], revision=1)
    inherited = service.add_node(
        plan.id, kind="task", title="Inherited detail", intent_node_id=canonical.id,
        acceptance_criteria="The inherited request is satisfied", db=db,
    )

    detail = read_current_status(actor=ACTOR, node_id=inherited.id, db=db)

    assert detail["canonical_intent"]["node_id"] == canonical.id
    assert detail["canonical_intent"]["title"] == "Canonical intent"
    assert detail["canonical_intent"]["intent"] == "Keep the request aligned"
    words = detail["canonical_intent"]["intent_words"]
    assert len(words) == 3
    assert {"author", "role", "quote", "added_at"} <= words[0].keys()
    assert all(len(word["quote"].encode()) <= 240 for word in words)
    assert any(word["role"] == "origin" for word in words)
