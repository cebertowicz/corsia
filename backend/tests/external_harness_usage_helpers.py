"""Shared fixtures for external-harness usage tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from infra.thread_driver import TerminalResult, TerminalSettlement


_USAGE_SELECT = (
    "SELECT model, prompt_tokens, completion_tokens, "
    "cache_creation_tokens, cache_read_tokens, total_tokens, source FROM llm_usage"
)


def usage_row():
    """The ledger's one row. Only valid where exactly one receipt is expected."""
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(_USAGE_SELECT).fetchone()
    finally:
        db.close()


def usage_rows():
    """Every ledger row, for a Turn expected to receipt more than one call."""
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(_USAGE_SELECT).fetchall()
    finally:
        db.close()


def persist_turn(thread_id: str, turn_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?,'user','t','t')",
            (thread_id,),
        )
        db.execute(
            """INSERT INTO active_turns
               (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
               VALUES (?,?,0,'test-model','running','t','test-boot')""",
            (turn_id, thread_id),
        )
        db.commit()
    finally:
        db.close()


def persist_settleable_turn(thread_id: str, turn_id: str) -> None:
    """A running Turn on a thread with a selected model, so ``finish_turn`` can settle it."""
    from infra.database import get_db

    persist_turn(thread_id, turn_id)
    db = get_db()
    try:
        db.execute("UPDATE threads SET model='test-model' WHERE id=?", (thread_id,))
        db.commit()
    finally:
        db.close()


def bind_plan_turn(thread_id: str, turn_id: str) -> tuple[str, ...]:
    from infra.database import get_db
    from plans import admission, service

    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        node = service.add_node(
            plan.id,
            kind="task",
            title=f"Bound {turn_id}",
            acceptance_criteria="Provider usage is attributed",
            db=db,
        )
        admission.activate(
            node.id,
            expected_revision=node.revision,
            actor_id=thread_id,
            provenance="test.external_harness_usage",
            db=db,
        )
        db.commit()
        row = db.execute(
            """SELECT plan_id,plan_node_id,plan_attempt_id,
                      plan_commitment_id,plan_usage_role
               FROM active_turns WHERE id=?""",
            (turn_id,),
        ).fetchone()
        assert row is not None
        return tuple(row)
    finally:
        db.close()


def usage_binding() -> tuple[str, ...]:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            """SELECT plan_id,plan_node_id,plan_attempt_id,
                      plan_commitment_id,plan_usage_role
               FROM llm_usage"""
        ).fetchone()
        assert row is not None
        return tuple(row)
    finally:
        db.close()


def stub_cc_terminal(monkeypatch, thread_id: str = "cc-session") -> None:
    settlement = TerminalSettlement(TerminalResult.APPLIED, thread_id)
    monkeypatch.setattr(
        "subagents.cc_event_result.claim_terminal",
        AsyncMock(return_value=settlement),
    )
    monkeypatch.setattr(
        "core.turn_core.drive_turn_settlement",
        AsyncMock(return_value=TerminalResult.APPLIED),
    )


def stub_codex_terminal(monkeypatch, thread_id: str = "codex-session") -> None:
    settlement = TerminalSettlement(TerminalResult.APPLIED, thread_id)
    monkeypatch.setattr(
        "subagents.codex_runtime_events.commit_turn_terminal",
        AsyncMock(return_value=settlement),
    )
    monkeypatch.setattr(
        "subagents.codex_runtime_events.release_and_drive_terminal",
        AsyncMock(),
    )
