"""Intent-word resolution does a bounded amount of work per call."""
from __future__ import annotations

import pytest

from infra.db_fts import _init_messages_fts
from plans import intent_words, service
from plans.boundary import mutate
from tests.plan_scene import ACTOR, db  # noqa: F401


def _doc_comment(db, comment_id: str, body: str, created_at: str) -> None:
    db.execute(
        """INSERT OR IGNORE INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES ('bounds-doc','local','document','bounds-doc','active','{}','t','t')"""
    )
    db.execute(
        """INSERT OR IGNORE INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES ('bounds-doc-registration','bounds-doc','root','owner-active','t','t')"""
    )
    db.execute(
        """INSERT INTO doc_comments (id,artifact_id,author,author_kind,body,created_at,updated_at)
           VALUES (?,'bounds-doc','Steve','user',?,?,?)""",
        (comment_id, body, created_at, created_at),
    )
    db.commit()


def _add(db, words, mutation_id: str):
    plan = service.ensure_plan("thread", "root", db=db)
    return mutate(
        plan.id, "add", {
            "title": "Bounded", "acceptance_criteria": "Words are bounded", "size": "S",
            "intent": "Keep the work bounded", "intent_words": words,
        }, actor=ACTOR, provenance="test", mutation_id=mutation_id, db=db,
    ).result


def test_more_than_three_quotes_in_one_call_are_refused_with_the_next_step(db):
    words = [{"quote": f"Agent word {index}.", "author": "agent"} for index in range(4)]

    with pytest.raises(service.PlanInputError) as refusal:
        _add(db, words, "too-many-quotes")

    assert str(refusal.value) == "intent_words takes at most 3 quotes per call; keep the strongest 3"


def test_a_quote_reads_at_most_one_bounded_page_of_candidates_per_source_kind(db, monkeypatch):
    from plans import person_input

    _init_messages_fts(db)
    for index in range(30):
        _doc_comment(db, f"near-{index}", f"Keep the narrowest scope {index}.", f"2026-10-{index + 1:02d}")
    loads = []
    original = person_input.load
    monkeypatch.setattr(person_input, "load", lambda *args: loads.append(args[1]) or original(*args))

    with pytest.raises(service.PlanInputError):
        _add(db, [{"quote": "Keep the narrowest scope wide.", "role": "authority"}], "bounded-reads")

    assert loads == ["doc_comment"] * intent_words._QUOTE_CANDIDATES


def test_an_exact_older_comment_is_found_behind_many_newer_unrelated_ones(db):
    _init_messages_fts(db)
    _doc_comment(db, "older-comment", "Ship the   exact\nolder wording.", "2026-01-01")
    for index in range(25):
        _doc_comment(db, f"newer-{index}", f"Unrelated note {index}.", f"2026-10-{index + 1:02d}")

    result = _add(db, [{"quote": "Ship the exact older wording.", "role": "authority"}], "older-quote")

    row = db.execute(
        "SELECT source_kind,source_id FROM plan_node_intent_words WHERE node_id=?", (result["node"]["id"],),
    ).fetchone()
    assert tuple(row) == ("doc_comment", "older-comment")


def test_newest_keeps_three_words_when_the_task_has_no_origin(db):
    plan = service.ensure_plan("thread", "root", db=db)
    node = service.add_node(plan.id, kind="task", title="Words", acceptance_criteria="Read", intent="Read", db=db)
    for index in range(6):
        intent_words.append(db, node.id, [{
            "quote": f"Word {index}", "author": "person", "role": "bears_on",
            "source_kind": "message", "source_id": f"m-{index}",
        }], revision=1)

    assert [word["quote"] for word in intent_words.newest(db, node.id)] == ["Word 5", "Word 4", "Word 3"]


def test_editing_the_intent_of_an_inheriting_node_makes_it_own_its_intent(db):
    plan = service.ensure_plan("thread", "root", db=db)
    canonical = service.add_node(
        plan.id, kind="task", title="Canonical", acceptance_criteria="Kept", intent="The shared goal", db=db,
    )
    inheriting = service.add_node(
        plan.id, kind="task", title="Inherits", acceptance_criteria="Follows", intent_node_id=canonical.id, db=db,
    )
    assert intent_words.canonical(db, inheriting.id)["id"] == canonical.id

    mutate(
        plan.id, "edit", {
            "node_id": inheriting.id, "intent": "A goal of its own", "size": "S",
            "expected_revision": service.get_node(inheriting.id, db=db).revision,
        }, actor=ACTOR, provenance="test", mutation_id="own-intent", db=db,
    )

    owned = intent_words.canonical(db, inheriting.id)
    assert owned["id"] == inheriting.id and owned["intent"] == "A goal of its own"
    assert service.get_node(inheriting.id, db=db).intent_node_id is None
