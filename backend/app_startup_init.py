"""Synchronous initialization before backend services start."""

import asyncio
import time

from infra import key_store_values
from infra.database import init_db
from tools.private_runtime_files import sweep_at_startup as sweep_private_launch_files


def initialize_startup(logger) -> None:
    from app_startup_workspace import initialize_workspace
    from connections.account_projection import repair_moved_projections
    from core.project_path_conversion import convert_legacy_project_paths
    from infra.config import LOGS_DIR, RUNTIME_DIR, UPLOADS_DIR, WORKSPACE, WORKSPACE_SELECTION
    from infra import setup_progress
    from infra.ignored_environment import drop_ignored_environment, note_ignored_environment
    from infra.held_replies import recover_interrupted as recover_interrupted_held_replies
    from infra.moved_state_paths import heal_moved_state_paths
    from infra.startup_prerequisites import prepare_startup_prerequisites
    from plans.public import sweep_owed_status
    from registry.durable_writes import recover_write_intents
    from registry.relocate import recover_interrupted_relocations

    note_ignored_environment()
    prepare_startup_prerequisites()
    for directory in (UPLOADS_DIR, LOGS_DIR, RUNTIME_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    phase_started = time.monotonic()
    logger.info("startup phase: database initialization started")
    init_db()
    heal_moved_state_paths()
    repair_moved_projections()
    drop_ignored_environment()
    setup_progress.release_marker_at_startup(logger)
    key_store_values.sweep_at_startup()
    sweep_private_launch_files()
    convert_legacy_project_paths(WORKSPACE_SELECTION)
    elapsed = time.monotonic() - phase_started
    logger.info("startup phase: database initialization complete in {:.1f}s", elapsed)
    recover_write_intents()
    recover_interrupted_relocations()
    recover_interrupted_held_replies()
    sweep_owed_status()
    initialize_workspace(WORKSPACE, logger)


async def initialize_startup_off_loop(logger) -> None:
    """Run :func:`initialize_startup` on a worker thread, with nothing else started yet.

    Its steps are synchronous SQLite work: an outermost ``BEGIN IMMEDIATE`` on
    the event-loop thread waits out the busy timeout while every request
    freezes, which is what ``infra/db_loop_guard`` reports. Two things change
    by moving it, both intended:

    * ``infra/write_admission._wait_bound`` gives a contended write on a worker
      thread 30 s instead of the 5 s a loop-thread write may spend waiting.
    * ``infra.database.get_db`` is thread-local, so init now writes on the
      worker's own pooled connection, as every ``run_in_db`` job already does.

    Nothing else does: none of the modules these steps live in imports asyncio,
    ``infra.app_loop`` still has no captured loop here (``lifespan`` captures it
    after this returns), and the sequence inside ``initialize_startup`` is
    untouched, so startup still finishes before anything else starts.
    """
    await asyncio.to_thread(initialize_startup, logger)
