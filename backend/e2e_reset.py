"""Test-only reset of the UI-E2E backend to the seed captured before the first spec.

The suite runs every spec file serially against one backend, so state one file leaves
(threads, an ended first run, open comment batches, settings, in-memory sessions) would
otherwise reach the next. ``capture_seed`` records the database, state root and workspace
once the app is up; ``restart_from_seed`` replaces this process with a fresh one that
restores that seed before it opens anything. The database cannot be put back under a
running app, so the reset is a restart: the process keeps its pid and listening port, and a
caller tells the new process apart by ``BOOT_ID``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from infra.adoption_snapshot import create_adoption_snapshot, restore_adoption_snapshot

#: Names the process that is serving now; a restart gives the next process its own.
BOOT_ID = uuid.uuid4().hex
RESTORE_ENV = "CORSIA_E2E_RESTORE_SEED"
_SEED_NAME = "e2e-seed"
# Lets the HTTP response that asked for the restart reach the caller before the process is replaced.
_RESPONSE_FLUSH_SECONDS = 0.3
_DESCRIPTOR_SCAN_LIMIT = 4096


def _roots() -> tuple[Path, Path]:
    """The disposable state root and workspace this E2E backend was started on."""
    try:
        return Path(os.environ["CORSIA_STATE_DIR"]).resolve(), Path(os.environ["CORSIA_WORKSPACE"]).resolve()
    except KeyError as missing:
        raise RuntimeError(f"E2E seed needs {missing.args[0]} set to the disposable root") from None


def _clear(directory: Path) -> None:
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def capture_seed() -> Path:
    """Record the running app's database, state root and workspace as the seed every reset returns to."""
    state_root, workspace = _roots()
    seed = state_root / "backups" / _SEED_NAME
    shutil.rmtree(seed, ignore_errors=True)
    snapshot = create_adoption_snapshot(state_root)
    snapshot.rename(seed)
    shutil.copytree(workspace, seed / "workspace", symlinks=True)
    return seed


def restore_if_requested() -> None:
    """Put the captured seed back; the first thing a process restarted by ``restart_from_seed`` does."""
    if os.environ.pop(RESTORE_ENV, None) is None:
        return
    state_root, workspace = _roots()
    seed = state_root / "backups" / _SEED_NAME
    if not (seed / "workspace").is_dir():
        raise RuntimeError(f"E2E seed restore found no captured seed at {seed}")
    restore_adoption_snapshot(state_root, seed)
    _clear(workspace)
    shutil.copytree(seed / "workspace", workspace, symlinks=True, dirs_exist_ok=True)


def restart_from_seed() -> None:
    """Replace this process with a fresh one on the same command line that restores the seed first."""
    seed = _roots()[0] / "backups" / _SEED_NAME
    if not seed.is_dir():
        raise RuntimeError("E2E reset needs a captured seed: POST /api/test/seed first")

    def replace_process() -> None:
        time.sleep(_RESPONSE_FLUSH_SECONDS)
        os.environ[RESTORE_ENV] = "1"
        # uvicorn marks its listening socket inheritable, and an exec would keep it open and block the new bind.
        # Mark every descriptor close-on-exec instead of closing it: the event loop still polls its own.
        for descriptor in range(3, _DESCRIPTOR_SCAN_LIMIT):
            try:
                os.set_inheritable(descriptor, False)
            except OSError:
                continue
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])

    threading.Thread(target=replace_process, name="e2e-reset", daemon=True).start()


router = APIRouter()


@router.post("/seed")
async def capture_seed_route() -> dict:
    """Record the running app's state as the seed ``POST /api/test/reset`` returns to.

    The suite calls it once, after warm-up and before its first spec.
    """
    await asyncio.to_thread(capture_seed)
    return {"ok": True, "boot_id": BOOT_ID}


@router.post("/reset")
async def reset_to_seed_route() -> dict:
    """Restart this backend from the captured seed: database, state root, workspace and all in-memory state.

    The response comes back first; the caller polls ``GET /api/test/boot-id`` until it names a new process.
    """
    try:
        restart_from_seed()
    except RuntimeError as refused:
        raise HTTPException(409, str(refused)) from refused
    return {"ok": True, "restarting_boot_id": BOOT_ID}


@router.get("/boot-id")
async def read_boot_id() -> dict:
    """Name the serving process; it changes once ``POST /api/test/reset`` has replaced it."""
    return {"boot_id": BOOT_ID}
