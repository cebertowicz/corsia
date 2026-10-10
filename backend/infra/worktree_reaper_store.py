"""Worktrees whose PR merged inside the grace period, for the nightly pass.

The PR hook defers a worktree that changed in the last 30 minutes. The
nightly pass then reaps it once idle for an hour, but only while its HEAD is
still the one recorded at merge time; anything newer falls back to the normal
seven-day threshold. The record lives under the state root, so a backend
restart does not forget it; a missing or unreadable record only means the
longer threshold applies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from loguru import logger

_LOCK = threading.Lock()


def _store_path() -> Path:
    from infra.config import STATE_ROOT

    return STATE_ROOT / "worktree_reaper" / "pr_terminal.json"


def _read() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict[str, object]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    live = {key: value for key, value in data.items() if os.path.lexists(key)}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pr_terminal.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(live, handle, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise


def remember_pr_terminal(worktree: Path, head: str) -> None:
    """Record that ``worktree`` at ``head`` belongs to a merged PR."""
    with _LOCK:
        data = _read()
        data[str(worktree)] = {"head": head, "recorded_at": time.time()}
        try:
            _write(data)
        except OSError as exc:
            logger.warning("worktree_reaper: could not record deferral: {}", exc)


def forget_pr_terminal(worktree: Path) -> None:
    """Drop a record once its worktree is gone."""
    with _LOCK:
        data = _read()
        if data.pop(str(worktree), None) is None:
            return
        try:
            _write(data)
        except OSError as exc:
            logger.warning("worktree_reaper: could not clear deferral: {}", exc)


def pr_terminal_heads() -> dict[str, str]:
    """Return recorded worktree paths mapped to their merge-time HEAD."""
    with _LOCK:
        data = _read()
    return {
        key: str(value["head"])
        for key, value in data.items()
        if isinstance(value, dict) and isinstance(value.get("head"), str)
    }
