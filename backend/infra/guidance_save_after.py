"""What follows a history save once its outcome is known; :mod:`infra.guidance_git` owns the save itself."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from loguru import logger

from infra.guidance_save_types import SaveResult
from infra.guidance_targets import HistoryTarget


def after_save(
    result: SaveResult, *, workspace: Path, writes: dict[Path, bytes], target: HistoryTarget, author: str,
    timing: dict[str, float], lesson_changes, lesson_observation_thread_id: str | None,
) -> SaveResult:
    """Report the save, then do the instruction-only follow-up a landed guidance commit owes (the lesson
    observation, the carry-in record). A document gets neither, and keeps a line only when a version
    could not be recorded or it was not written. The log line
    and product event name the kind, so guidance and document saves stay apart in the metrics."""
    if target.document and result.outcome in ("written", "unchanged", "history-off", "ordinary") and not result.unsynced:
        # A document's writer has nothing to act on when its version was recorded, or never would be.
        result = replace(result, line=None)
    from infra.product_events import emit_product_event

    properties = {
        "outcome": result.outcome, "scope": target.scope, "history_off": result.outcome == "history-off",
        "index_unsynced": list(result.unsynced), **timing,
    }
    logger.info("{} save {}", target.kind, properties)
    emit_product_event(f"{target.kind}_history", "save", properties=properties)
    warnings: list[str | None] = []
    if result.outcome == "written" and lesson_changes:
        from infra.lesson_save_gate import observe_written_save

        warnings.append(observe_written_save(
            lesson_changes, author, stewardship_thread_id=lesson_observation_thread_id,
        ))
    if result.outcome == "written" and result.sha and writes and not target.document:
        # Only a landed guidance commit creates C1 work; matching remains off this request path.
        from core.carry_in_facts import record_committed_save
        from infra.carry_in_drain import request_drain

        record_committed_save(workspace, list(writes), result.sha)
        request_drain(workspace)
    if any(warnings):
        result = SaveResult(
            result.outcome, result.reason, result.sha,
            line="\n".join(line for line in (result.line, *warnings) if line),
            unsynced=result.unsynced, errors=result.errors,
        )
    return result
