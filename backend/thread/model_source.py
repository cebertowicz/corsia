"""Source identity, ownership, and replay candidates for model selection."""

from __future__ import annotations

from typing import Any

from loguru import logger

from thread.model_destination import (
    destination_identity_changes,
    field_changes,
    refreshed_fields,
    require_dispatchable_destination,
)
from thread.model_row import _model_switch_boundary_is_clear, _model_switch_transcript_version
from thread.model_projection import read_model_switch_fit_input, read_model_switch_input_proof
from thread.model_state import (
    ModelSwitchConflict,
    ModelSwitchPreparationError,
    ModelSwitchSequencingRequired,
)
from thread.transcript_order import (
    destructive_boundary_version,
)


def _execution_target(model: str | None, *, db=None):
    if not model:
        return None
    from llm.resolution import resolve_execution_target

    return resolve_execution_target(model, db=db)


def _current_switch_destination(db, destination_model: str, prepared, *, thread_id: str, stage: str):
    """Re-resolve the chosen destination; only a refresh of its facts passes.

    Every switch stage that rechecks its destination comes here. An identity
    change is a conflict, an unavailable destination fails with its reason,
    and the current resolution is returned so fit uses the window it has now.
    """
    from llm.resolution import ModelUnavailableError

    try:
        current = _execution_target(destination_model, db=db)
    except ModelUnavailableError as exc:
        raise ModelSwitchPreparationError(
            f"model switch destination is unavailable: {exc.reason}"
        ) from exc
    except ValueError as exc:
        raise ModelSwitchPreparationError(
            f"model switch destination resolution failed: {exc}"
        ) from exc
    changed = destination_identity_changes(prepared, current)
    if changed:
        logger.warning(
            "model switch destination identity changed {}: thread={} destination={} {}",
            stage, thread_id, destination_model, field_changes(prepared, current, changed),
        )
        raise ModelSwitchConflict(f"model switch destination changed {stage}")
    require_dispatchable_destination(current)
    refreshed = refreshed_fields(prepared, current)
    if refreshed:
        logger.info(
            "model switch destination refreshed {}: thread={} destination={} {}",
            stage, thread_id, destination_model, field_changes(prepared, current, refreshed),
        )
    return current


async def _capture_model_switch_source_owner(
    thread_id: str,
    source_harness: str | None,
) -> Any | None:
    if source_harness == "claude-code":
        from subagents import cc_runtime

        return await cc_runtime.capture_retirement_owner(thread_id)
    if source_harness == "codex":
        from subagents import codex_runtime

        return await codex_runtime.capture_retirement_owner(thread_id)
    return None


def _read_owned_model_switch_thread(
    db,
    thread_id: str,
    *,
    user_id: str | None,
    allow_active_turn: bool,
):
    row = db.execute(
        "SELECT s.user_id,s.model,s.project_id,s.runtime_context_epoch,"
        "(SELECT id FROM active_turns "
        " WHERE thread_id=s.id AND status='running' LIMIT 1) "
        "AS active_turn_id "
        "FROM threads s WHERE s.id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise ModelSwitchPreparationError(
            f"model switch thread does not exist: {thread_id}"
        )
    if user_id is not None and row["user_id"] != user_id:
        raise ModelSwitchPreparationError(
            f"model switch thread is not owned by {user_id}"
        )
    active_turn_id = (
        str(row["active_turn_id"]) if row["active_turn_id"] is not None else None
    )
    if active_turn_id is not None and not allow_active_turn:
        raise ModelSwitchSequencingRequired(
            f"model switch blocked by active Turn {active_turn_id}"
        )
    return row, active_turn_id


def _resolve_model_switch_targets(
    db,
    *,
    source_model: str,
    destination_model: str,
):
    try:
        destination_target = _execution_target(destination_model, db=db)
    except Exception as exc:
        raise ModelSwitchPreparationError(
            f"model switch destination resolution failed: {exc}"
        ) from exc
    try:
        source_target = _execution_target(source_model, db=db)
    except Exception:
        # Re-addressability must still repair a thread whose old catalog row
        # was removed. Runtime ownership is resolved on the event loop before
        # any source continuation is quiesced.
        source_target = None
    require_dispatchable_destination(destination_target)
    return source_target, destination_target


def _read_model_switch_start(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    allow_active_turn: bool = False,
) -> tuple[
    str | None,
    str,
    Any,
    Any,
    str | None,
    int,
    tuple[int, int, int],
    str | None,
]:
    """Read the owned source/destination identities before any runtime action."""
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("BEGIN")
        row, active_turn_id = _read_owned_model_switch_thread(
            db,
            thread_id,
            user_id=user_id,
            allow_active_turn=allow_active_turn,
        )
        boundary_version = destructive_boundary_version(db, thread_id)
        source_selected_model = str(row["model"] or "").strip() or None
        if source_selected_model is None:
            # A first selection has nothing to migrate from: ``switch_model``
            # sets it directly and never prepares a switch.
            raise ModelSwitchPreparationError(
                "model switch source has no selected model"
            )
        source_model = source_selected_model
        if source_selected_model == destination_model:
            return (
                source_selected_model,
                source_model,
                None,
                None,
                row["project_id"],
                int(row["runtime_context_epoch"]),
                boundary_version,
                active_turn_id,
            )
        source_target, destination_target = _resolve_model_switch_targets(
            db,
            source_model=source_model,
            destination_model=destination_model,
        )
        return (
            source_selected_model,
            source_model,
            source_target,
            destination_target,
            row["project_id"],
            int(row["runtime_context_epoch"]),
            boundary_version,
            active_turn_id,
        )
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def _unresolved_source_harness(thread_id: str) -> str | None:
    from subagents import cc_runtime, codex_runtime

    live_harnesses = [
        harness
        for harness, is_live in (
            ("claude-code", cc_runtime.has_pending_or_live(thread_id)),
            ("codex", codex_runtime.has_pending_or_live(thread_id)),
        )
        if is_live
    ]
    if len(live_harnesses) > 1:
        raise ModelSwitchPreparationError(
            "model switch source is unresolved and has ambiguous live "
            "Claude Code and Codex runtime owners"
        )
    return live_harnesses[0] if live_harnesses else None


def _validate_model_switch_candidate_boundary(
    db,
    thread_id: str,
    *,
    user_id: str | None,
    source_selected_model: str | None,
    project_id: str | None,
    expected_version: tuple[int, int, int],
    allow_active_turn: bool,
) -> None:
    row = db.execute(
        "SELECT user_id,model,project_id,history_epoch FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise ModelSwitchPreparationError(
            "model switch thread vanished during preparation"
        )
    if user_id is not None and row["user_id"] != user_id:
        raise ModelSwitchPreparationError(
            "model switch thread ownership changed during preparation"
        )
    if (str(row["model"] or "").strip() or None) != source_selected_model:
        raise ModelSwitchConflict("model switch source model changed during preparation")
    if int(row["history_epoch"]) != expected_version[0]:
        raise ModelSwitchConflict("model switch history epoch changed during preparation")
    if row["project_id"] != project_id:
        raise ModelSwitchConflict("model switch project scope changed during preparation")
    boundary_clear = _model_switch_boundary_is_clear(db, thread_id, expected_version)
    if boundary_clear or (
        allow_active_turn
        and destructive_boundary_version(db, thread_id) == expected_version
    ):
        return
    active = db.execute(
        "SELECT 1 FROM active_turns "
        "WHERE thread_id = ? AND status='running' LIMIT 1",
        (thread_id,),
    ).fetchone()
    if active is not None:
        raise ModelSwitchSequencingRequired("model switch blocked by an active Turn")
    raise ModelSwitchSequencingRequired(
        "model switch destructive boundary changed during preparation"
    )


def _read_model_switch_candidates(
    thread_id: str,
    *,
    user_id: str | None,
    source_selected_model: str | None,
    source_model: str,
    project_id: str | None,
    expected_version: tuple[int, int, int],
    destination_model: str,
    destination_target: Any,
    allow_active_turn: bool = False,
    max_items: int | None = None,
):
    from core.context_replay import read_replay_candidates
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("BEGIN")
        _validate_model_switch_candidate_boundary(
            db,
            thread_id,
            user_id=user_id,
            source_selected_model=source_selected_model,
            project_id=project_id,
            expected_version=expected_version,
            allow_active_turn=allow_active_turn,
        )
        current_target = _current_switch_destination(
            db, destination_model, destination_target,
            thread_id=thread_id, stage="during preparation",
        )
        candidates = read_replay_candidates(thread_id, db=db)
        transcript_version = _model_switch_transcript_version(db, thread_id)
        fit_input = read_model_switch_fit_input(
            db,
            thread_id,
            destination_model=destination_model,
            destination_target=current_target,
            max_items=max_items,
        )
        input_proof = read_model_switch_input_proof(db, thread_id, fit_input)
        db.rollback()
        return candidates, transcript_version, fit_input, input_proof, current_target
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


async def _live_same_harness_context_usage(
    thread_id: str,
    *,
    source_harness: str | None,
    source_target: Any,
    destination_target: Any,
    source_runtime_context_epoch: int,
) -> int | float | None:
    """Read exact live same-harness usage, or return no proof."""
    if (
        source_harness not in {"claude-code", "codex"}
        or source_target is None
        or getattr(destination_target, "harness", None) != source_harness
    ):
        return None
    from infra.context_receipts import (
        current_context_receipt_identity,
        latest_measured_context_receipt,
    )
    from infra.db_async import run_in_db

    identity = current_context_receipt_identity(
        thread_id,
        harness=source_harness,
        catalog_model_id=source_target.catalog_id,
        runtime_context_epoch=source_runtime_context_epoch,
    )
    if identity is None:
        return None

    def _read_usage():
        return latest_measured_context_receipt(
            thread_id,
            identity,
        )

    record = await run_in_db(_read_usage)
    if record is None:
        return None
    total_used = record["context_usage"].get("total_used")
    if isinstance(total_used, bool) or not isinstance(total_used, (int, float)):
        return None
    return total_used
