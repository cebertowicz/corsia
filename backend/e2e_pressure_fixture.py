"""Repairable and irreparable context-pressure fixtures for mock browser E2E."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

# Use the smallest production-scale cap covered by the checkpoint contract. The
# cold writer's deliberately conservative byte bound for the real tool surface
# cannot fit the former synthetic 53,248-token window at all. History must still
# cross the pressure threshold while each individual turn remains small enough
# for the cold fitter to retain a meaningful suffix.
PRESSURE_CONTEXT_WINDOW = 131_072
PRESSURE_MODEL_BY_HARNESS = {
    "native": "mock-pressure-model",
    "claude-code": "claude-code/fixture-cc-pressure",
    "codex": "codex/fixture-codex-pressure",
}
# The mock alias uses the shared labelled character estimate.
_PRESSURE_HISTORY_ASSISTANT_REPEATS = 7_581
_PRESSURE_HISTORY_TURNS = 12

router = APIRouter()


def _seed_pressure_rows(thread_id: str, user_id: str) -> tuple[int, str]:
    """Select the pressure model and insert completed, session-unique turns."""
    from infra.database import get_db
    from infra.time import utc_now_iso

    db = get_db()
    try:
        session = db.execute(
            "SELECT model FROM threads WHERE id = ? AND user_id = ?",
            (thread_id, user_id),
        ).fetchone()
        if session is None:
            raise LookupError("thread not found")
        now = utc_now_iso()
        current_model = str(session["model"])
        from llm.resolution import resolve_execution_target

        harness = resolve_execution_target(current_model, db=db).harness
        model = PRESSURE_MODEL_BY_HARNESS[harness]
        db.execute(
            "UPDATE threads SET model = ?, updated_at = ? WHERE id = ?",
            (model, now, thread_id),
        )
        # Twelve 30,324-character bodies total about 121,300 estimated tokens.
        # With the advertised tool surface (about 9,700 since #1939 shrank the
        # plan schemas) the new input lands mid-way between the pressure
        # threshold and the window, so a tool-schema change of a few thousand
        # tokens cannot move it out. Each turn remains small enough for a cold
        # suffix.
        for number in range(1, _PRESSURE_HISTORY_TURNS + 1):
            turn_id = f"{thread_id}-e2e-pressure-{number}"
            metadata = json.dumps({"turn_id": turn_id})
            db.execute(
                "INSERT INTO active_turns "
                "(id,thread_id,execution_epoch,model,status,started_at,ended_at,"
                "boot_epoch) VALUES (?,?,0,?,'completed',?,?,'e2e-pressure')",
                (turn_id, thread_id, model, now, now),
            )
            db.executemany(
                "INSERT INTO messages "
                "(id,thread_id,role,content,metadata,created_at,seq) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        f"{thread_id}-e2e-pressure-user-{number}",
                        thread_id,
                        "user",
                        "prior request",
                        metadata,
                        now,
                        number * 2 - 1,
                    ),
                    (
                        f"{thread_id}-e2e-pressure-assistant-{number}",
                        thread_id,
                        "assistant",
                        "old " * _PRESSURE_HISTORY_ASSISTANT_REPEATS,
                        metadata,
                        now,
                        number * 2,
                    ),
                ],
            )
        db.execute(
            "UPDATE threads SET next_message_seq = ?, updated_at = ? WHERE id = ?",
            (_PRESSURE_HISTORY_TURNS * 2 + 1, now, thread_id),
        )
        db.commit()
        return _PRESSURE_HISTORY_TURNS, model
    finally:
        db.close()


@router.post("/seed-pressured-history")
async def seed_pressured_history(token: str, body: dict) -> dict:
    """Seed completed history that forces the production checkpoint gate."""
    from infra.auth import authenticate
    from infra.db_async import run_in_db

    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    thread_id = str(body.get("thread_id") or "")
    if not thread_id:
        raise HTTPException(400, "thread_id is required")
    try:
        turns, model = await run_in_db(_seed_pressure_rows, thread_id, user["id"])
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "ok": True,
        "turns": turns,
        "model": model,
        "context_window": PRESSURE_CONTEXT_WINDOW,
    }
