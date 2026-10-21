"""Workspace-bound startup work that must not prevent the backend from serving."""

from __future__ import annotations

from pathlib import Path


def initialize_workspace(workspace: Path, logger) -> None:
    """Refresh workspace projections, convert skill keys and data, sweep, assign owners, then backfill."""
    from core.skill_data_conversion import convert_legacy_skill_data
    from core.skill_key_conversion import convert_legacy_skill_keys
    from core.system_projection import ensure_system_projection
    from infra.guidance_git import startup_sweep
    from registry import (
        finished_task_row_backfill,
        ownership_row_backfill,
        taskless_row_backfill,
    )
    from registry.shared_ownership_assignment import run_startup_assignment

    ensure_system_projection(workspace)
    try:
        convert_legacy_skill_keys(workspace)
    except Exception:
        logger.exception("skill frontmatter key conversion failed; retrying next start")
    try:
        convert_legacy_skill_data(workspace)
    except Exception:
        logger.exception("skill CORSIA data conversion failed; retrying next start")
    startup_sweep(workspace)
    try:
        from core.lesson_carry_in import schedule_pending_drain

        schedule_pending_drain(workspace)
    except Exception:
        logger.exception("pending carry-in startup drain could not start")
    try:
        run_startup_assignment(workspace)
    except Exception:
        logger.exception(
            "shared ownership assignment startup pass failed; retrying next start"
        )
    # In rule order: each pass reads what the one before it left.
    for label, backfill in (
        ("ownership assignment", ownership_row_backfill),
        ("finished-task", finished_task_row_backfill),
        ("taskless", taskless_row_backfill),
    ):
        try:
            backfill.run_startup_backfill()
        except Exception:
            logger.exception("%s pane-row backfill failed; retrying next start", label)
