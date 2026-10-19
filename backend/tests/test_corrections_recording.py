"""Focused behavior tests for the local correction-classifier recorder."""

from __future__ import annotations

import json

import pytest


def _db():
    from infra.database import get_db

    return get_db()


def _seed(db, *, message_id="owner-message", created_at="2026-10-01T00:01:00Z"):
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','project','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,created_at,updated_at) VALUES ('thread','owner','thread','project','t','t')"
    )
    db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('prior','thread','assistant','Prior answer.','{}','2026-10-01T00:00:00Z')")
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)",
        (message_id, "thread", "user", "That result is wrong.", "{}", created_at),
    )
    db.commit()


def _version():
    from core.corrections_recording import ClassifierVersion

    return ClassifierVersion("corrections-classifier", "v1", "correction", 0.595681, "modernbert-owner-context-v2-2048")


def test_owner_message_filter_excludes_agents_and_system_wrappers():
    from core.corrections_recording import is_owner_authored_message

    assert not is_owner_authored_message(role="user", content="x", metadata=json.dumps({"sender_kind": "agent"}))
    assert not is_owner_authored_message(role="user", content="[EVENT: tick]", metadata="{}")
    assert is_owner_authored_message(role="user", content="That is wrong.", metadata="{}")


def test_arrival_trigger_only_notifies_new_owner_messages(monkeypatch):
    from jobs import corrections_realtime

    seen = []
    monkeypatch.setattr(corrections_realtime, "notify", lambda source_kind, source_id: seen.append((source_kind, source_id)))

    def accepted(message_id, duplicate=False):
        from types import SimpleNamespace

        return SimpleNamespace(message_id=message_id, duplicate=duplicate)

    corrections_realtime.notify_if_owner(accepted("message"), role="user", content="A correction.", metadata="{}")
    corrections_realtime.notify_if_owner(accepted("duplicate", True), role="user", content="A correction.", metadata="{}")
    corrections_realtime.notify_if_owner(
        accepted("agent"), role="user", content="A correction.", metadata={"sender_kind": "agent"},
    )

    assert seen == [("message", "message")]


@pytest.mark.asyncio
async def test_scan_hydrates_recent_context_and_uses_versioned_dedupe(isolated_db):
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    keys = pending_candidate_keys(_version(), 20, "owner")
    candidates = hydrate_candidates(keys)

    assert [candidate.source_id for candidate in candidates] == ["owner-message"]
    assert candidates[0].context == (("Agent", "Prior answer."),)


@pytest.mark.asyncio
async def test_context_is_shaped_and_bounded_like_the_training_rows(isolated_db):
    from core.corrections_input import CONTEXT_MESSAGE_CHARS, OWNER_MESSAGE_CHARS
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db, created_at="2026-10-01T00:09:00Z")
        rows = [
            ("a1", "assistant", "  oldest  ", "{}"),
            ("u1", "user", "x" * 5000 + "END", "{}"),
            ("w1", "user", "[EVENT: tick] wrapped", "{}"),
            ("r1", "user", "routed", '{"routed_event": "e"}'),
            ("t1", "tool", "tool output", "{}"),
            ("a2", "assistant", "newest", "{}"),
        ]
        for index, (message_id, role, content, metadata) in enumerate(rows):
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)",
                (message_id, "thread", role, content, metadata, f"2026-10-01T00:0{index + 2}:00Z"),
            )
        db.execute("UPDATE messages SET content=? WHERE id='owner-message'", ("y" * (OWNER_MESSAGE_CHARS + 10),))
        db.commit()
    finally:
        db.close()

    candidate = hydrate_candidates(pending_candidate_keys(_version(), 20, "owner"))[0]

    # Wrappers, routed deliveries and non-chat roles are skipped; oldest first; Agent/User labels.
    assert [role for role, _ in candidate.context] == ["Agent", "Agent", "User", "Agent"]
    assert candidate.context[1] == ("Agent", "oldest")
    assert candidate.context[2] == ("User", ("x" * 5000 + "END")[-CONTEXT_MESSAGE_CHARS:])
    assert len(candidate.content) == OWNER_MESSAGE_CHARS


def test_context_keeps_the_newest_six_within_the_total_character_cap():
    from core.corrections_input import shape_context

    rows = [("user", f"m{index}", f"m{index}", "{}") for index in range(8)]
    assert [text for _, text in shape_context(rows)] == ["m5", "m4", "m3", "m2", "m1", "m0"]
    # Prose, not one long run: the mask replaces a long unbroken token before the cap counts it.
    big = [("assistant", "a", "word " * 300, "{}")] * 6
    assert len(shape_context(big)) == 5  # 5 x 1,499 fits 8,000; a sixth would not


def test_context_skips_only_the_deliveries_training_skipped():
    from core.corrections_input import shape_context

    rows = [
        ("user", "brief", "brief", json.dumps({"sender_kind": "agent"})),
        ("user", "sent", "sent", json.dumps({"sender_kind": "agent", "send_idempotency_key": "k"})),
        ("user", "[AGENT-SEND from x]", "[AGENT-SEND from x]", json.dumps({"sender_kind": "agent"})),
    ]
    # Training kept agent-sent rows unless a delivery key or wrapper prefix marked them.
    assert shape_context(rows) == (("User", "brief"),)


@pytest.mark.asyncio
async def test_unreadable_fact_names_its_failure_and_a_scored_fact_has_none(isolated_db):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    candidate = hydrate_candidates(pending_candidate_keys(_version(), 20, "owner"))[0]
    with pytest.raises(ValueError):
        _record_fact(candidate, {"label": "unreadable", "truncated": False, "original_tokens": 0, "failure_reason": "none"}, _version())
    with pytest.raises(ValueError):
        _record_fact(candidate, {"label": "correction", "truncated": False, "original_tokens": 3, "failure_reason": "rss"}, _version())
    assert _record_fact(candidate, {"label": "unreadable", "truncated": False, "original_tokens": 0, "failure_reason": "timeout"}, _version())[0]

    db = _db()
    try:
        attributes = json.loads(db.execute("SELECT attributes FROM outcome_events").fetchone()[0])
    finally:
        db.close()
    assert (attributes["label"], attributes["failure_reason"]) == ("unreadable", "timeout")
    # The message is no longer pending for this version.
    assert pending_candidate_keys(_version(), 20, "owner") == []


def test_unreadable_count_reads_one_version_inside_the_measurement_window(isolated_db):
    from datetime import datetime, timezone

    from core.corrections_recording import ClassifierVersion, CorrectionCandidate
    from jobs.corrections_recording import _record_fact, unreadable_messages

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    recent = datetime.now(timezone.utc).isoformat()
    unreadable = {"label": "unreadable", "truncated": False, "original_tokens": 0, "failure_reason": "timeout"}
    newer = ClassifierVersion("corrections-classifier", "v2", "correction", 0.5, "modernbert-owner-context-v2-2048")

    def candidate(message_id, created_at):
        return CorrectionCandidate("message", message_id, "thread", "project", "x", created_at)

    assert _record_fact(candidate("recent", recent), unreadable, _version())[0]
    assert _record_fact(candidate("recent", recent), unreadable, newer)[0]  # re-read by a newer version
    assert _record_fact(candidate("old", "2000-01-01T00:00:00Z"), unreadable, _version())[0]  # outside the window

    db = _db()
    try:
        assert unreadable_messages(db, _version()) == 1
        assert unreadable_messages(db, newer) == 1
    finally:
        db.close()


def test_one_encoding_round_trips_the_version_identity():
    from core.corrections_recording import decode_version, version_attributes, version_key

    version = _version()
    assert decode_version(version_attributes(version)) == version
    assert version_key(version) == "corrections-classifier:v1:correction:0.595681:modernbert-owner-context-v2-2048"
    assert version.__class__("a", "v", "h", 0.1234567, "p").threshold == 0.123457


@pytest.mark.asyncio
async def test_fact_carries_the_verified_asset_result_without_message_text(isolated_db):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    candidate = hydrate_candidates(pending_candidate_keys(_version(), 20, "owner"))[0]
    assert _record_fact(candidate, {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}, _version())[0]

    db = _db()
    try:
        attributes = json.loads(db.execute("SELECT attributes FROM outcome_events").fetchone()[0])
    finally:
        db.close()
    assert attributes == {
        "source_kind": "message", "source_id": "owner-message", "thread_id": "thread", "label": "correction",
        "asset_id": "corrections-classifier", "asset_version": "v1", "head": "correction",
        "threshold": "0.595681", "input_policy_id": "modernbert-owner-context-v2-2048",
        "truncated": "false", "original_tokens": 9, "failure_reason": "none",
        "classified_at": attributes["classified_at"],
    }


@pytest.mark.asyncio
async def test_same_message_and_version_is_recorded_once_when_scan_and_notify_race(isolated_db):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_candidate_keys

    db = _db()
    try:
        _seed(db)
    finally:
        db.close()
    candidate = hydrate_candidates(pending_candidate_keys(_version(), 20, "owner"))[0]
    payload = {"label": "not_correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}
    assert _record_fact(candidate, payload, _version())[0]
    assert not _record_fact(candidate, payload, _version())[0]


def test_only_send_thread_input_calls_the_corrections_admission_hook():
    """Plan wakes, event delivery, digests and spawn paths cannot enqueue classification."""
    import ast
    from pathlib import Path

    backend = Path(__file__).parents[1]
    callers = []
    for path in backend.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "notify_if_owner"
                for node in ast.walk(function)
            ):
                callers.append((path.relative_to(backend).as_posix(), function.name))

    assert callers == [("infra/send_service.py", "send_thread_input")]


def _seed_comment_and_answer_sources(db):
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES ('doc','local','document','doc','active','{}','t','t')"""
    )
    db.execute(
        """INSERT INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES ('doc-owner','doc','thread','owner-active','t','t')"""
    )
    db.execute(
        """INSERT INTO doc_comments
           (id,artifact_id,author,author_kind,body,anchor_text,created_at,updated_at)
           VALUES ('comment','doc','owner','user','The date is wrong.','2027 launch','2026-10-01T00:02:00Z','t')"""
    )
    decision = {
        "plan_decision": {
            "state": "answered", "thread_id": "thread", "revision": 7,
            "answered_at": "2026-10-01T00:03:00Z", "title": "Which date?",
            "context": "Release copy", "tradeoff": "Accuracy", "options": [],
            "answer": {"text": "Use 2027, not 2026."},
        },
    }
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES ('decision-artifact','local','plan-decision','decision','active',?,'t','t')""",
        (json.dumps(decision),),
    )
    db.commit()


@pytest.mark.asyncio
async def test_scan_batches_messages_comments_and_free_text_answers(isolated_db):
    from jobs.corrections_recording import next_pending_batch

    db = _db()
    try:
        _seed(db)
        _seed_comment_and_answer_sources(db)
    finally:
        db.close()

    batch = await next_pending_batch(_version(), 10, None, since="2026-10-01T00:00:00Z")

    assert [(key.source_kind, key.source_id) for key in batch.selected_keys] == [
        ("plan_decision_answer", "decision"), ("doc_comment", "comment"), ("message", "owner-message"),
    ]
    assert [(candidate.source_kind, candidate.source_id) for candidate in batch.candidates] == [
        ("plan_decision_answer", "decision:7"), ("doc_comment", "comment"), ("message", "owner-message"),
    ]


def test_measurement_figure_excludes_comment_and_decision_facts(isolated_db):
    from datetime import datetime, timezone

    from core.corrections_recording import CorrectionCandidate
    from jobs.corrections_activation import weekly_correction_figure
    from jobs.corrections_recording import _record_fact

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {"label": "correction", "truncated": False, "original_tokens": 1, "failure_reason": "none"}
    assert _record_fact(CorrectionCandidate("message", "message", "thread", "project", "Wrong.", timestamp), payload, _version())[0]
    assert _record_fact(CorrectionCandidate("doc_comment", "comment", "thread", "project", "Wrong.", timestamp), payload, _version())[0]
    assert _record_fact(CorrectionCandidate("plan_decision_answer", "decision:7", "thread", "project", "Wrong.", timestamp), payload, _version())[0]

    assert weekly_correction_figure(_version(), since="2000-01-01T00:00:00Z", weeks=1) == 2
