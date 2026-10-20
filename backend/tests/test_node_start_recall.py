"""Prior-word source and exclusion behavior."""

from __future__ import annotations

import json

import pytest
from loguru import logger

from infra.database import get_db
from tests.event_routing_helpers import mk_session


@pytest.fixture
def db(isolated_db):
    from infra.db_fts import _init_fts

    connection = get_db()
    _init_fts(connection)
    mk_session("caller")
    mk_session("past")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def logged():
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    yield lines
    logger.remove(sink)


def _message(db, message_id: str, thread: str, body: str, *, role="user", metadata="{}", seq=1, created_at="2026-01-01T12:00:00+00:00"):
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,delivery_state) "
        "VALUES (?,?,?,?,?,?,?,'delivered')",
        (message_id, thread, role, body, metadata, created_at, seq),
    )
    db.commit()


def _related(db, title="Export account ledger in CSV format"):
    from memory.node_start_recall import NodeText, related

    return related(db, NodeText(title, criteria="Use stable export columns."), caller_thread_id="caller", trigger="start")


def test_owner_message_is_quoted_with_header_and_newest_first(db, logged):
    _message(db, "old", "past", "Export account ledger as CSV format with stable columns.")
    _message(db, "new", "past", "Export account format is preferred.", seq=2, created_at="2026-02-01T12:00:00+00:00")

    lines = _related(db)

    assert lines[0].startswith("Earlier words from the person")
    assert len(lines) == 3
    assert "preferred" in lines[1] and "as CSV" in lines[2]
    assert any("prior_words reason=delivered trigger=start hits=2 kinds=message:2" in line for line in logged)


def test_agent_send_and_routed_event_are_never_quoted(db):
    _message(db, "agent", "past", "Export account ledger as CSV format.", role="assistant")
    _message(db, "event", "past", "Export account ledger as CSV format.", metadata='{"sender_kind":"agent"}', seq=2)

    assert _related(db) == []


def test_user_comment_targets_its_owner_without_an_author_thread_and_excludes_agent_comment(db):
    from doc_feedback_helpers import add_comment, seed_document

    db.execute("UPDATE threads SET user_id='owner-token'")
    artifact = seed_document(db, "past", name="ledger-guide.md")
    person, _ = add_comment(db, artifact, author_kind="user", author_id=None)
    agent, _ = add_comment(db, artifact, author_kind="agent", author_id="past")
    db.execute("UPDATE doc_comments SET body='Export account ledger with CSV format.' WHERE id IN (?, ?)", (person["id"], agent["id"]))
    db.commit()

    lines = _related(db)

    assert person["author_thread_id"] is None
    assert len(lines) == 2 and "comment on “ledger-guide.md”" in lines[1]


def test_decision_without_free_text_is_not_quoted(db):
    from plans.public import DECISION_TYPE

    metadata = {"plan_decision": {"state": "answered", "thread_id": "past", "answered_at": "2026-01-01", "title": "Export ledger", "answer": {"option_id": "csv"}}}
    db.execute(
        "INSERT INTO artifact_identities (id,integration,type,external_id,state,retention,metadata,created_at,updated_at) "
        "VALUES ('decision','local',?,'decision','active','durable',?,'2026-01-01','2026-01-01')",
        (DECISION_TYPE, json.dumps(metadata)),
    )
    db.commit()

    assert _related(db) == []


def test_many_newer_one_term_comments_do_not_hide_an_older_match(db):
    from doc_feedback_helpers import add_comment, seed_document
    from memory.node_start_recall import SOURCE_CANDIDATES

    db.execute("UPDATE threads SET user_id='owner-token'")
    artifact = seed_document(db, "past", name="ledger-guide.md")
    match, _ = add_comment(db, artifact, author_kind="user", author_id=None)
    noise = [add_comment(db, artifact, author_kind="user", author_id=None)[0]["id"] for _ in range(SOURCE_CANDIDATES + 1)]
    db.execute("UPDATE doc_comments SET body='Export account ledger with CSV format.', created_at='2026-01-01' WHERE id=?", (match["id"],))
    db.executemany("UPDATE doc_comments SET body='Export later.', created_at='2026-02-01' WHERE id=?", [(comment_id,) for comment_id in noise])
    db.commit()

    lines = _related(db)

    assert len(lines) == 2 and "Export account ledger with CSV" in lines[1]


def test_purged_thread_answered_decision_is_not_quoted(db):
    from plans.public import DECISION_TYPE

    metadata = {"plan_decision": {"state": "answered", "thread_id": "past", "answered_at": "2026-01-01", "title": "Export ledger", "answer": {"option_id": "other", "text": "Export account ledger with CSV format."}}}
    db.execute(
        "INSERT INTO artifact_identities (id,integration,type,external_id,state,retention,metadata,created_at,updated_at) "
        "VALUES ('purged-decision','local',?,'purged-decision','active','durable',?,'2026-01-01','2026-01-01')",
        (DECISION_TYPE, json.dumps(metadata)),
    )
    db.execute("UPDATE threads SET purged_at='2026-01-02' WHERE id='past'")
    db.commit()

    assert _related(db) == []


def test_many_one_term_decision_answers_do_not_hide_a_match(db):
    from memory.node_start_recall import SOURCE_CANDIDATES
    from plans.public import DECISION_TYPE

    def decision(decision_id: str, text: str, answered_at: str) -> tuple:
        body = {"state": "answered", "thread_id": "past", "answered_at": answered_at, "title": "Ledger", "answer": {"option_id": "other", "text": text}}
        return (decision_id, DECISION_TYPE, decision_id, json.dumps({"plan_decision": body}))

    noise = [decision(f"noise-{index}", "Export later.", "2026-02-01") for index in range(SOURCE_CANDIDATES + 1)]
    rows = [*noise, decision("z-match", "Export account ledger with CSV format.", "2026-01-01")]
    db.executemany(
        "INSERT INTO artifact_identities (id,integration,type,external_id,state,retention,metadata,created_at,updated_at) "
        "VALUES (?,'local',?,?,'active','durable',?,'2026-01-01','2026-01-01')", rows,
    )
    db.commit()

    lines = _related(db)

    assert len(lines) == 2 and "answer to decision" in lines[1]


def test_locality_is_read_once_per_project(db):
    from memory.node_start_recall import SOURCE_CANDIDATES

    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('one','one','2026-01-01','2026-01-01')")
    db.execute("UPDATE threads SET project_id='one'")
    for index in range(SOURCE_CANDIDATES):
        _message(db, f"message-{index}", "past", "Export account ledger with CSV format.", seq=index + 1)
    reads: list[str] = []
    db.set_trace_callback(lambda sql: reads.append(sql) if "project_inference_policies" in sql else None)

    lines = _related(db)

    db.set_trace_callback(None)
    assert len(lines) == 4
    assert len(reads) == 1


def test_only_user_sourced_active_rule_is_quoted(db):
    from checkpoint_retrieval_fixtures import _install_checkpoint

    _install_checkpoint(db, thread_id="past", narrative="Rule", continuity=[{
        "entry_id": 1,
        "field": "active_constraints",
        "body": {"statement": "Export account ledger as CSV format.", "source_refs": [{"kind": "user", "reference": "message"}]},
        "revision": 1,
    }])

    lines = _related(db)

    assert len(lines) == 2 and "rule an agent recorded" in lines[1]


def test_rule_without_revision_is_quoted(db):
    from checkpoint_retrieval_fixtures import _install_checkpoint

    _install_checkpoint(db, thread_id="past", narrative="Rule", continuity=[{
        "entry_id": 1,
        "field": "active_constraints",
        "body": {"statement": "Export account ledger as CSV format.", "source_refs": [{"kind": "user", "reference": "message"}]},
    }])

    assert any("rule an agent recorded" in line for line in _related(db))


def test_retired_rule_is_not_quoted(db):
    from checkpoint_retrieval_fixtures import _install_checkpoint

    _install_checkpoint(db, thread_id="past", narrative="Rule", continuity=[{
        "entry_id": 1,
        "field": "active_constraints",
        "body": {"statement": "Export account ledger as CSV format.", "source_refs": [{"kind": "user", "reference": "message"}]},
        "revision": 1,
    }])
    db.execute(
        "UPDATE continuity_entries SET state='retired', retired_reason='replaced' "
        "WHERE thread_id='past' AND entry_id=1",
    )
    db.commit()

    assert _related(db) == []


def test_callers_own_live_rule_is_not_quoted(db):
    from checkpoint_retrieval_fixtures import _install_checkpoint

    _install_checkpoint(db, thread_id="caller", narrative="Rule", continuity=[{
        "entry_id": 1,
        "field": "active_constraints",
        "body": {"statement": "Export account ledger as CSV format.", "source_refs": [{"kind": "user", "reference": "message"}]},
        "revision": 1,
    }])

    assert _related(db) == []


def test_live_transcript_messages_are_excluded_but_folded_messages_are_eligible(db):
    db.execute(
        "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,status,source_harness,source_model,writer_harness,writer_model,continuity_base_revision,continuity_base_snapshot_json,started_at,installed_at) "
        "VALUES ('checkpoint','caller',0,1,'folded','manual','cold','installed','native','m','native','m',0,'[]','2026-01-01','2026-01-01')"
    )
    _message(db, "folded", "caller", "Export account ledger with CSV format.", seq=1)
    _message(db, "live", "caller", "Export account ledger with CSV format.", seq=2)

    lines = _related(db)

    assert len(lines) == 2
    assert 'get_thread_history(thread_id="caller")' in lines[1]


def test_cloud_off_message_stays_in_its_project(db):
    from llm.locality import set_project_cloud_models_off

    db.executemany(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
        [("one", "one", "2026-01-01", "2026-01-01"), ("two", "two", "2026-01-01", "2026-01-01")],
    )
    db.execute("UPDATE threads SET project_id='one' WHERE id='past'")
    db.execute("UPDATE threads SET project_id='two' WHERE id='caller'")
    set_project_cloud_models_off("one", cloud_models_off=True, db=db)
    _message(db, "project-message", "past", "Export account ledger with CSV format.")

    assert _related(db) == []
    db.execute("UPDATE threads SET project_id='one' WHERE id='caller'")
    db.commit()
    assert len(_related(db)) == 2


def test_limits_to_three_lines_and_one_rule(db):
    from checkpoint_retrieval_fixtures import _install_checkpoint

    _install_checkpoint(db, thread_id="past", narrative="Rules", continuity=[
        {"entry_id": 1, "field": "active_constraints", "body": {"statement": "Export account ledger as CSV format.", "source_refs": [{"kind": "user", "reference": "first"}]}, "revision": 1},
        {"entry_id": 2, "field": "approvals_and_preferences", "body": {"statement": "Export account ledger CSV format stays stable.", "source_refs": [{"kind": "user", "reference": "second"}]}, "revision": 1},
    ])
    for index in range(3):
        _message(db, f"message-{index}", "past", "Export account ledger with CSV format.", seq=index + 1, created_at=f"2026-02-0{index + 1}T00:00:00+00:00")

    lines = _related(db)

    assert len(lines) == 4
    assert sum("rule an agent recorded" in line for line in lines) == 1


def test_words_after_task_start_are_excluded(db):
    from memory.node_start_recall import NodeText, related
    from plans import service
    from tests.plan_execution_helpers import add_task

    plan = service.ensure_plan("thread", "caller", db=db)
    task = add_task(db, plan.id, "Export account ledger in CSV format")
    db.commit()
    _message(db, "future", "past", "Export account ledger with CSV format.", created_at="2099-01-01T00:00:00+00:00")

    assert related(db, NodeText(task.title, criteria=task.acceptance_criteria, node_id=task.id), caller_thread_id="caller", trigger="start") == []


def test_no_terms_and_below_floor_log_their_reasons(db, logged):
    assert _related(db, "Fix tests") == []
    _message(db, "partial", "past", "Export only.")
    assert _related(db) == []

    assert any("reason=no_terms" in line for line in logged)
    assert any("reason=below_floor" in line for line in logged)


def test_error_isolated_and_key_stays_absent(db, logged):
    db.execute("DROP TABLE messages")

    assert _related(db) == []
    assert any("prior_words reason=error" in line for line in logged)
