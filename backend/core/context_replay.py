"""Replay selection and explicit history-loss declarations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from history.types import HistoryBoundary, PortableHistoryError, PortableHistoryProjection

_RECOVERY_HINT = (
    "If you need something that is missing, recover it before assuming it does "
    "not exist: search this thread's history, read its past checkpoints and "
    "its plan, or ask for the specific detail you need."
)


@dataclass(frozen=True)
class ReplaySelection:
    """One exact model-facing history choice for a destination."""

    mode: Literal["exact", "checkpoint_tail"]
    history_epoch: int
    sealed_boundary: HistoryBoundary | None
    tail: PortableHistoryProjection
    checkpoint_generation_id: str | None = None
    checkpoint_boundary: HistoryBoundary | None = None
    checkpoint_json: str | None = None


def exact_replay(projection: PortableHistoryProjection) -> ReplaySelection:
    """Select a seq-0 projection as exact replay; a scoped tail never qualifies."""
    if projection.start_exclusive is not None:
        raise PortableHistoryError("a checkpoint-scoped history is not an exact replay")
    return ReplaySelection(
        "exact", projection.history_epoch, projection.sealed_boundary, projection,
    )


@dataclass(frozen=True)
class ReplayCandidates:
    """Canonical replay frozen in one snapshot, with its seq-0 identity seal.

    Installed coverage replaces exact archive replay; it is not a fallback
    ranked by fit. Without coverage, retained raw history remains diagnostic
    ``last_resort`` only and cannot silently become an exact replay.
    """

    exact: ReplaySelection | None
    checkpoint_tail: ReplaySelection | None
    history_epoch: int
    sealed_boundary: HistoryBoundary | None
    last_resort: ReplaySelection | None


def _dropped_range_statement(selection: ReplaySelection) -> str:
    start = selection.tail.start_exclusive
    if start is None:
        return ""
    covered = selection.checkpoint_boundary.seq if selection.checkpoint_boundary else 0
    if start.seq <= covered:
        return ""
    return (
        f"History between position {covered} and position {start.seq} of this "
        "thread was DROPPED to fit this model's context window. It is not "
        "summarized anywhere in what you were given."
    )


def _retention_trim_statement(selection: ReplaySelection) -> str:
    tail = selection.tail
    start = tail.start_exclusive
    sealed = tail.sealed_boundary
    trimmed = [
        seq
        for seq in tail.retention_stub_seqs
        if (start is None or seq > start.seq) and (sealed is None or seq <= sealed.seq)
    ]
    if not trimmed:
        return ""
    return (
        f"{len(trimmed)} tool result(s) in the history you were given are "
        "PLACEHOLDERS. Their output was discarded by retention. The calls did "
        "run, so re-run a tool only when its CURRENT output is needed."
    )


def _missing_result_statement(selection: ReplaySelection) -> str:
    tail = selection.tail
    start = tail.start_exclusive
    sealed = tail.sealed_boundary
    missing = [
        seq
        for seq in tail.missing_result_stub_seqs
        if (start is None or seq > start.seq) and (sealed is None or seq <= sealed.seq)
    ]
    if not missing:
        return ""
    return (
        f"{len(missing)} tool result(s) in the history you were given were "
        "NEVER RECORDED. Verify current state before assuming either outcome."
    )


def history_loss_notice(selection: ReplaySelection) -> str:
    """Declare every hole carried by a replay selection."""
    statements = tuple(
        filter(
            None,
            (
                _dropped_range_statement(selection),
                _retention_trim_statement(selection),
                _missing_result_statement(selection),
            ),
        )
    )
    if not statements:
        return ""
    return (
        "[CORSIA incomplete history]\n"
        + "\n".join(statements)
        + "\n"
        + _RECOVERY_HINT
        + "\n"
    )


def checkpoint_prompt(selection: ReplaySelection) -> str:
    """Render an installed checkpoint followed by any history-loss declaration."""
    notice = history_loss_notice(selection)
    if selection.mode != "checkpoint_tail" or selection.checkpoint_json is None:
        return notice
    boundary = selection.checkpoint_boundary
    identity = {
        "generation_id": selection.checkpoint_generation_id,
        "history_epoch": boundary.history_epoch if boundary else None,
        "through_seq": boundary.seq if boundary else None,
        "through_message_id": boundary.message_id if boundary else None,
    }
    return (
        notice
        + "[CORSIA checkpoint]\n"
        + "Prior thread context through the stated boundary. Continue from it "
        + "together with the exact tail that follows.\n"
        + json.dumps(identity, sort_keys=True, separators=(",", ":"))
        + "\n"
        + selection.checkpoint_json
    )


def _read_replay_candidates_snapshot(
    thread_id: str,
    *,
    db,
    exclude_message_id: str | None,
) -> ReplayCandidates:
    from core.checkpoint_resume import installed_replay_with_exact_seal
    from history.projection import render_portable_history
    from infra.checkpoint_queries import current_installed

    installed = current_installed(thread_id, db=db)
    if installed is not None:
        tail, seal = installed_replay_with_exact_seal(
            installed, db=db, exclude_message_id=exclude_message_id,
        )
        return ReplayCandidates(None, tail, tail.history_epoch, seal, tail)
    full = render_portable_history(
        thread_id,
        db=db,
        exclude_message_id=exclude_message_id,
    )
    raw = ReplaySelection("exact", full.history_epoch, full.sealed_boundary, full)
    return ReplayCandidates(
        exact=None if full.retention_stub_seqs else raw,
        checkpoint_tail=None,
        history_epoch=full.history_epoch,
        sealed_boundary=full.sealed_boundary,
        last_resort=raw,
    )


def read_replay_candidates(
    thread_id: str,
    *,
    db,
    exclude_message_id: str | None = None,
) -> ReplayCandidates:
    """Read every replay choice inside one exact savepoint."""
    savepoint = "context_preflight_replay_candidates"
    db.execute(f"SAVEPOINT {savepoint}")
    try:
        candidates = _read_replay_candidates_snapshot(
            thread_id,
            db=db,
            exclude_message_id=exclude_message_id,
        )
        db.execute(f"RELEASE {savepoint}")
        return candidates
    except Exception:
        db.execute(f"ROLLBACK TO {savepoint}")
        db.execute(f"RELEASE {savepoint}")
        raise
