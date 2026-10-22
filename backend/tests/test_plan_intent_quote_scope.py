"""A quoted person's words resolve through the one reader and the one scope rule Context & Memory applies."""
from __future__ import annotations

import pytest

from infra.db_fts import _init_messages_fts
from infra.input_admission import accept_thread_input
from plans import decisions, intent_words, person_input, service
from plans.boundary import PlanActor, mutate
from tests.plan_scene import ACTOR, _mid, db, raise_decision  # noqa: F401

PERSON = PlanActor(None, "user-1", None, kind="user")


def _thread(db, thread_id: str, *, user: str = "user-1", project: str | None = "project-1") -> str:
    db.execute(
        """INSERT INTO threads (id,user_id,project_id,lifecycle_state,title,model,created_at,updated_at)
           VALUES (?,?,?,'active',?,'claude-code/sonnet','t','t')""", (thread_id, user, project, thread_id),
    )
    return thread_id


def _say(db, thread: str, text: str, message_id: str, **extra) -> str:
    accept_thread_input(thread, role="user", content=text, intent="apply_now", message_id=message_id, db=db, **extra)
    return message_id


def _resolve(db, quote: str, *, author: str = "person", actor_id: str | None = "root") -> list[dict]:
    return intent_words.resolve_words(db, [{"quote": quote, "author": author, "role": "authority"}], actor_id=actor_id)


@pytest.fixture
def words_db(db):
    _init_messages_fts(db)
    return db


def test_a_decision_answer_quote_resolves_to_its_answered_revision(words_db):
    db = words_db
    decision = raise_decision(db, "root", "root")
    decisions.answer(
        decision["id"], option_id="other", other_text="Ship the narrow exporter before the ledger",
        expected_revision=decision["revision"], mutation_id=_mid(), actor=PERSON, provenance="test", db=db,
    )
    db.commit()

    node = mutate(
        service.ensure_plan("thread", "root", db=db).id, "add", {
            "title": "Narrow exporter", "acceptance_criteria": "It ships first", "size": "S",
            "intent": "Ship the narrow exporter first",
            "intent_words": [{"quote": "narrow exporter before the ledger", "role": "authority"}],
        }, actor=ACTOR, provenance="test", mutation_id="decision-quote", db=db,
    ).result["node"]

    row = db.execute(
        "SELECT source_kind,source_id FROM plan_node_intent_words WHERE node_id=? AND role='authority'", (node["id"],),
    ).fetchone()
    loaded = person_input.load(db, "plan_decision_answer", decision["id"])
    assert (row["source_kind"], row["source_id"]) == ("plan_decision_answer", loaded.id)
    assert loaded.id.startswith(decision["id"] + ":")


def test_the_same_scope_resolves_and_every_other_scope_is_refused_alike(words_db):
    db = words_db
    _thread(db, "purged")
    _thread(db, "local")
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-3','Off','t','t')")
    db.execute("INSERT INTO project_inference_policies (project_id,cloud_models_off) VALUES ('project-3',1)")
    _thread(db, "cloud-off", project="project-3")
    _say(db, "other-root", "Keep the scoped exporter honest.", "same-scope")
    _say(db, "foreign", "Keep the foreign exporter honest.", "other-user")
    _say(db, "purged", "Keep the purged exporter honest.", "purged-row")
    _say(db, "local", "Keep the local exporter honest.", "local-row")
    _say(db, "cloud-off", "Keep the cloud-off exporter honest.", "cloud-off-row")
    db.execute("UPDATE threads SET purged_at='t' WHERE id='purged'")
    db.execute("UPDATE threads SET local_only=1 WHERE id='local'")
    db.commit()

    assert [(word["source_kind"], word["source_id"]) for word in _resolve(db, "scoped exporter honest")] == [
        ("message", "same-scope"),
    ]
    refusals = set()
    for quote in ("foreign exporter", "purged exporter", "local exporter", "cloud-off exporter", "never said"):
        with pytest.raises(service.PlanInputError) as refusal:
            _resolve(db, quote)
        refusals.add(str(refusal.value))
    assert refusals == {intent_words.QUOTE_NOT_FOUND}


def test_a_local_only_chat_still_reaches_a_local_only_thread(words_db):
    db = words_db
    _thread(db, "local")
    _say(db, "local", "Keep the local exporter honest.", "local-row")
    db.execute("UPDATE threads SET local_only=1 WHERE id IN ('local','root')")
    db.commit()

    assert _resolve(db, "local exporter")[0]["source_id"] == "local-row"


def _comment(db, comment_id: str, body: str, *, owner: str, recipient: str) -> None:
    artifact = f"doc-{comment_id}"
    db.execute(
        """INSERT INTO artifact_identities (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES (?,'local','document',?,'active','{}','t','t')""", (artifact, artifact),
    )
    db.execute(
        """INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES (?,?,?,'owner-active','t','t')""", (f"reg-{comment_id}", artifact, owner),
    )
    db.execute(
        """INSERT INTO doc_comments (id,artifact_id,author,author_kind,body,created_at,updated_at)
           VALUES (?,?,'Steve','user',?,'t','t')""", (comment_id, artifact, body),
    )
    db.execute(
        """INSERT INTO doc_feedback_batches
           (id,artifact_id,author_kind,author_id,recipient_id,state,opened_at,last_item_at,closes_at,created_at,updated_at)
           VALUES (?,?,'user','user-1',?,'delivered','t','t','t','t','t')""", (f"batch-{comment_id}", artifact, recipient),
    )
    db.execute(
        "INSERT INTO doc_feedback_batch_items (batch_id,comment_id) VALUES (?,?)", (f"batch-{comment_id}", comment_id),
    )


def test_a_comment_belongs_to_the_agent_it_was_handed_to_not_the_documents_registration(words_db):
    db = words_db
    _comment(db, "handed-here", "Keep the release scope narrow.", owner="foreign", recipient="root")
    _comment(db, "handed-away", "Keep the archive scope wide.", owner="root", recipient="foreign")
    db.commit()

    assert [(word["source_kind"], word["source_id"]) for word in _resolve(db, "release scope narrow")] == [
        ("doc_comment", "handed-here"),
    ]
    with pytest.raises(service.PlanInputError, match="quote was not found"):
        _resolve(db, "archive scope wide")


def test_relays_events_and_blanks_are_not_the_persons_words_by_one_rule(words_db):
    db = words_db
    rows = {
        "plain": ("Build the exporter.", None),
        "event": ("[EVENT: reviewer.verdict] Build the exporter.", None),
        "relay": ("Build the exporter.", {"sender_kind": "agent"}),
        "routed": ("Build the exporter.", {"routed_event": "evt-1"}),
        "blank": ("   ", None),
    }
    for message_id, (content, metadata) in rows.items():
        _say(db, "root", content, message_id, metadata=metadata)
        db.execute("UPDATE messages SET active_turn_id=? WHERE id=?", (f"turn-{message_id}", message_id))

    for message_id in rows:
        loaded = person_input.load(db, "message", message_id) is not None
        assert loaded == intent_words.has_person_origin(db, f"turn-{message_id}") == (message_id == "plain")


def test_agent_words_are_a_plan_edit_by_the_acting_thread():
    assert [(word["source_kind"], word["source_id"]) for word in (
        *_resolve(None, "I will check the export.", author="agent"),
        *_resolve(None, "I will check the export.", author="agent", actor_id=None),
    )] == [("plan_edit", "root"), ("plan_edit", "system")]


def test_person_quotes_need_the_agents_own_thread(words_db):
    with pytest.raises(service.PlanInputError, match="recorded from the agent's own thread"):
        _resolve(words_db, "Build the exporter.", actor_id=None)
