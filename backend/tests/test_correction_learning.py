from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from core.correction_learning import loaded_targets, record_terminal_outcome, record_triage, serious_proposed


NOW = "2026-10-20T00:00:00+00:00"
THREAD = "thread"
TURN = "turn"
SUBJECT = "message:1"


def _db() -> sqlite3.Connection:
    from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
    from infra.schema import SCHEMA_SQL
    from infra.usage_schema import USAGE_SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.executescript(USAGE_SCHEMA_SQL)
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES (?, 'user', 'native/test', 0, 1, ?, ?)",
        (THREAD, NOW, NOW),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?, ?, 0, 'native/test', 'running', ?, 'boot')",
        (TURN, THREAD, NOW),
    )
    from infra.outcome_events import record_outcome_event

    record_outcome_event(
        db, dedupe_key="correction:1", event_name="measurement.correction", source="agent",
        provenance="agent_recorded", ts=NOW, subject_key=SUBJECT, agent_thread_id=THREAD,
        attributes={"label": "correction"},
    )
    return db


def _record_manifest(db: sqlite3.Connection, source) -> None:
    from core.effective_context import build_effective_context_snapshot
    from core.guidance_manifest import ManifestHeader, _write_manifest, build_guidance_manifest, stamp_raw_versions

    stamp_raw_versions([source])
    snapshot = build_effective_context_snapshot(
        thread_id=THREAD, reason="test", scope_facts={}, agent_facts={},
        harness_model_facts={"harness": "native", "model": "native/test"},
        cwd=None, source_root=None, sources=[source], history=[], current_user_content="test",
    )
    manifest = build_guidance_manifest(
        snapshot, ManifestHeader.for_selector(
            harness="native", selector="native/test", expected_reported_model=None,
            transport_model="native/test",
        ),
    )
    _write_manifest(db, manifest, NOW)
    db.execute("UPDATE active_turns SET context_manifest_id=? WHERE id=?", (manifest.id, TURN))


def _file_source(path: Path):
    from core.effective_context import ContextSourceEntry

    return ContextSourceEntry(
        source_id="project:rule", origin_section="project", label=path.name,
        stable_reference="thread://thread/project", freshness="fresh", inclusion="included",
        reason="file", owner_surface="document", prompt_content=path.read_text(encoding="utf-8"),
        metadata={"path": str(path)},
    )


def _generated_source():
    from core.effective_context import ContextSourceEntry

    return ContextSourceEntry(
        source_id="runtime:envelope", origin_section="dynamic", label="Envelope",
        stable_reference="corsia://runtime/envelope", freshness="fresh", inclusion="included",
        reason="generated", owner_surface="runtime", prompt_content="runtime facts",
    )


def _record_skill_read(db: sqlite3.Connection, monkeypatch, path: Path) -> None:
    import infra.database
    from core.guidance_manifest import record_guidance_load

    monkeypatch.setattr(infra.database, "write_transaction", lambda *_args, **_kwargs: nullcontext(db))
    record_guidance_load(
        thread_id=THREAD, turn_id=TURN, path=path, data=path.read_bytes(),
        returned_text=path.read_text(encoding="utf-8"), partial=False,
    )


def test_email_effect_proposes_serious_and_lower_tier_needs_reason(tmp_path: Path) -> None:
    from infra.queries import insert_message

    db = _db()
    path = tmp_path / "SKILL.md"
    path.write_text("# Rule\n", encoding="utf-8")
    _record_manifest(db, _file_source(path))
    insert_message(
        THREAD, "tool", "sent", {"turn_id": TURN, "name": "email_send"},
        msg_id="tool-email", db=db,
    )

    with pytest.raises(ValueError, match="Serious is proposed"):
        record_triage(
            db, thread_id=THREAD, subject_key=SUBJECT, tier="miss", cause="process_gap",
            new_work="no", new_work_signal=None, target_file=str(path),
        )
    result = record_triage(
        db, thread_id=THREAD, subject_key=SUBJECT, tier="serious", cause="process_gap",
        new_work="no", new_work_signal=None, target_file=str(path),
    )

    assert result["target_file"] == str(path)


def test_generated_manifest_entry_is_not_a_correction_target() -> None:
    db = _db()
    _record_manifest(db, _generated_source())

    assert loaded_targets(db, THREAD) == ()
    with pytest.raises(ValueError, match="no manifest or load-receipt files"):
        record_triage(
            db, thread_id=THREAD, subject_key=SUBJECT, tier="note", cause="process_gap",
            new_work="no", new_work_signal=None, target_file="corsia://runtime/envelope",
        )


def test_relative_manifest_and_resolved_read_share_one_target_path(tmp_path: Path, monkeypatch) -> None:
    import infra.config

    db = _db()
    path = tmp_path / "skills" / "example" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Example\n", encoding="utf-8")
    monkeypatch.setattr(infra.config, "WORKSPACE", tmp_path)
    source = _file_source(path)
    source.metadata["path"] = "skills/example/SKILL.md"
    _record_manifest(db, source)
    _record_skill_read(db, monkeypatch, path.resolve())

    assert loaded_targets(db, THREAD) == ("skills/example/SKILL.md",)
    result = record_triage(
        db, thread_id=THREAD, subject_key=SUBJECT, tier="note", cause="process_gap",
        new_work="no", new_work_signal=None, target_file="skills/example/SKILL.md",
    )

    assert result["target_file"] == "skills/example/SKILL.md"
    row = db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name='learning_loop.correction_triage'",
    ).fetchone()
    assert json.loads(row["attributes"])["target_file"] == "skills/example/SKILL.md"


def test_target_path_keeps_outside_workspace_files_absolute(tmp_path: Path) -> None:
    from tools.filesystem_gate import canonical_target_path

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside" / "SKILL.md"
    workspace.mkdir()
    outside.parent.mkdir()
    outside.write_text("# Outside\n", encoding="utf-8")

    assert canonical_target_path(str(outside), workspace=workspace) == str(outside.resolve())


def test_serious_effect_scope_is_the_corrected_work_not_the_triage_turn() -> None:
    from infra.queries import insert_message

    db = _db()
    insert_message(
        THREAD, "user", "earlier task", {}, msg_id="person-earlier", db=db,
        created_at="2026-10-19T20:00:00+00:00",
    )
    insert_message(
        THREAD, "tool", "old send", {"turn_id": "older-turn", "name": "email_send"},
        msg_id="tool-before-task", db=db, created_at="2026-10-19T21:00:00+00:00",
    )
    insert_message(
        THREAD, "user", "the task", {}, msg_id="person-task", db=db,
        created_at="2026-10-19T22:00:00+00:00",
    )

    # The only send ran before the task's person message: not part of the corrected work.
    assert serious_proposed(db, THREAD, SUBJECT) is False

    insert_message(
        THREAD, "tool", "task send", {"turn_id": "task-turn", "name": "email_send"},
        msg_id="tool-in-task", db=db, created_at="2026-10-19T23:00:00+00:00",
    )

    # A send in the turn before the correction counts, though it is not the running turn.
    assert serious_proposed(db, THREAD, SUBJECT) is True


def test_serious_effect_with_no_earlier_person_message_scans_from_the_thread_start() -> None:
    from infra.queries import insert_message

    db = _db()
    insert_message(
        THREAD, "tool", "send", {"turn_id": "first-turn", "name": "email_send"},
        msg_id="tool-first", db=db, created_at="2026-10-19T21:00:00+00:00",
    )

    assert serious_proposed(db, THREAD, SUBJECT) is True


@pytest.mark.asyncio
async def test_prefixed_serious_effect_from_native_result_writer_is_detected() -> None:
    from core.native_tool_result import handle_native_tool_complete
    from infra.queries import insert_message

    db = _db()
    insert_message(
        THREAD, "assistant", "", {"turn_id": TURN, "llm_tool_calls": [{
            "id": "call-email", "type": "function",
            "function": {"name": "mcp__corsia__email_send", "arguments": "{}"},
        }]}, msg_id="step-email", db=db,
    )

    async def run_in_db(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def emit(_event):
        return None

    handler = SimpleNamespace(
        tool_step_id="step-email", thread_id=THREAD, turn_id=TURN,
        tool_calls=[{"id": "call-email", "result": None}], _emit=emit,
    )
    row = await handle_native_tool_complete(
        handler,
        {"id": "call-email", "name": "mcp__corsia__email_send", "result": "sent"},
        (run_in_db, lambda *_args, **_kwargs: nullcontext(db)),
    )

    metadata = json.loads(db.execute("SELECT metadata FROM messages WHERE id=?", (row.message_id,)).fetchone()[0])
    assert metadata["name"] == "mcp__corsia__email_send"
    assert serious_proposed(db, THREAD, SUBJECT) is True


def test_not_a_correction_records_only_its_tier_even_after_a_serious_effect() -> None:
    from infra.queries import insert_message

    db = _db()
    insert_message(
        THREAD, "tool", "send", {"turn_id": TURN, "name": "email_send"}, msg_id="tool-send", db=db,
        created_at="2026-10-20T00:00:01+00:00",
    )
    with pytest.raises(ValueError, match="records only its tier"):
        record_triage(db, thread_id=THREAD, subject_key=SUBJECT, tier="not_correction", cause="process_gap")

    result = record_triage(db, thread_id=THREAD, subject_key=SUBJECT, tier="not_correction")

    attributes = json.loads(db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name='learning_loop.correction_triage'"
    ).fetchone()[0])
    assert result["target_file"] is None
    assert attributes["tier"] == "not_correction" and "cause" not in attributes
    assert "serious_proposed" not in attributes


def test_a_miss_needs_its_cause_but_not_a_loaded_file() -> None:
    db = _db()
    with pytest.raises(ValueError, match="cause is required"):
        record_triage(db, thread_id=THREAD, subject_key=SUBJECT, tier="miss", new_work="no")

    result = record_triage(
        db, thread_id=THREAD, subject_key=SUBJECT, tier="miss", cause="cant_be_done", new_work="no",
    )

    assert result["target_file"] is None


def test_serious_effect_with_unreadable_turn_provenance_is_refused_not_skipped() -> None:
    from infra.queries import insert_message

    db = _db()
    insert_message(
        THREAD, "tool", "send", {"turn_id": TURN, "name": "email_send"}, msg_id="tool-no-turn", db=db,
        created_at="2026-10-20T00:00:01+00:00",
    )
    db.execute("UPDATE messages SET active_turn_id=NULL, metadata=? WHERE id='tool-no-turn'", ('{"name": "email_send"}',))

    with pytest.raises(ValueError, match="whether Serious applies cannot be determined"):
        serious_proposed(db, THREAD, SUBJECT)


def test_unreadable_manifest_entries_are_refused_not_listed_as_empty(tmp_path: Path) -> None:
    guidance = tmp_path / "rule.md"
    guidance.write_text("rule", encoding="utf-8")
    db = _db()
    _record_manifest(db, _file_source(guidance))
    db.execute("UPDATE context_manifests SET entries_json='{\"not\": \"a list\"}'")

    with pytest.raises(ValueError, match="unreadable entries_json"):
        loaded_targets(db, THREAD)


def test_correction_subject_must_belong_to_the_calling_thread() -> None:
    from infra.outcome_events import record_outcome_event

    db = _db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES ('other', 'user', 'native/test', 0, 1, ?, ?)",
        (NOW, NOW),
    )
    record_outcome_event(
        db, dedupe_key="correction:other", event_name="measurement.correction", source="agent",
        provenance="agent_recorded", ts=NOW, subject_key="message:other", agent_thread_id="other",
        attributes={"label": "correction"},
    )

    with pytest.raises(ValueError, match="recorded correction for this thread"):
        record_terminal_outcome(db, thread_id=THREAD, subject_key="message:other", outcome="fixed")


def test_terminal_outcome_is_immutable_and_dropped_needs_reason(tmp_path: Path) -> None:
    db = _db()
    path = tmp_path / "SKILL.md"
    path.write_text("# Rule\n", encoding="utf-8")
    _record_manifest(db, _file_source(path))
    result = record_terminal_outcome(db, thread_id=THREAD, subject_key=SUBJECT, outcome="fixed")

    assert result["outcome"] == "fixed"
    with pytest.raises(ValueError, match="already exists"):
        record_terminal_outcome(db, thread_id=THREAD, subject_key=SUBJECT, outcome="watch")
    with pytest.raises(ValueError, match="requires reason"):
        record_terminal_outcome(db, thread_id=THREAD, subject_key="message:other", outcome="dropped")


def test_a_field_the_allowlist_drops_refuses_the_fact_instead_of_storing_it_without_the_field() -> None:
    too_long = "x" * 501

    # Each case gets its own database: the tool's write transaction rolls the refused fact back, a bare connection does not.
    with pytest.raises(ValueError, match="serious_lowered_reason was not accepted"):
        record_triage(
            _db(), thread_id=THREAD, subject_key=SUBJECT, tier="note", new_work="no", serious_lowered_reason=too_long,
        )
    with pytest.raises(ValueError, match="reason was not accepted"):
        record_terminal_outcome(_db(), thread_id=THREAD, subject_key=SUBJECT, outcome="dropped", reason=too_long)
    with pytest.raises(ValueError, match="lesson_id was not accepted"):
        record_terminal_outcome(_db(), thread_id=THREAD, subject_key=SUBJECT, outcome="lesson", lesson_id="has space")


def test_triage_and_outcome_run_through_the_continuity_tool(tmp_path: Path) -> None:
    from infra.continuity_store import ContinuityError
    from tools.continuity import _execution_error, _live_operation, _result_text

    db = _db()
    path = tmp_path / "SKILL.md"
    path.write_text("# Rule\n", encoding="utf-8")
    _record_manifest(db, _file_source(path))
    triage = {
        "subject_key": SUBJECT, "tier": "note", "cause": "process_gap", "new_work": "no",
        "new_work_signal": None, "target_file": str(path),
    }

    assert _execution_error("triage", {}) == "Error: triage requires subject_key"
    assert _execution_error("outcome", {"subject_key": " "}) == "Error: outcome requires subject_key"
    with pytest.raises(ContinuityError, match="target_file is not a file loaded"):
        _live_operation(db, THREAD, "triage", {**triage, "target_file": "elsewhere.md"})
    with pytest.raises(ContinuityError, match="not a recorded correction"):
        _live_operation(db, THREAD, "outcome", {"subject_key": "message:other", "outcome": "fixed"})

    recorded = _live_operation(db, THREAD, "triage", {"operation": "triage", **triage})
    assert json.loads(_result_text(recorded, staged=False))["operation"] == "triage"
    finished = _live_operation(db, THREAD, "outcome", {"operation": "outcome", "subject_key": SUBJECT, "outcome": "fixed"})
    assert json.loads(_result_text(finished, staged=False))["outcome"] == "fixed"
