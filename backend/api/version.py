"""Public version endpoint — backend + frontend build identity.

The frontend bundle has its OWN copy of build-info.json inlined at vite
build time (frozen). This endpoint exposes the SERVER's view of:
  - frontend: read from dist/build-info.json (what's currently deployed)
  - backend: captured once at server start (what the running process is),
    PLUS `current_head` re-read per request (what `git HEAD` is on disk
    right now — flags "process is running stale code, restart to pick up").

Comparing bundle-self vs server-frontend reveals stale browser caches.
Comparing backend.short_hash vs backend.current_head.short_hash reveals
a backend process that hasn't been restarted since new commits landed.
No auth — must be reachable to bootstrap the version overlay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from infra.config import FRONTEND_DIR
from infra.platform_paths import app_bundle_root

router = APIRouter(prefix="/api", tags=["version"])
log = logging.getLogger(__name__)

# Repo root: .corsia/. backend/ is a sibling of frontend/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# An app bundle has no checkout: its backend and frontend are one signed build.
_APP_BUNDLE = app_bundle_root()


def _git_read(args: list[str]) -> str | None:
    """Run a git command in the repo root, return stripped stdout, or None on failure.

    ``--no-optional-locks`` keeps ``git status`` from refreshing the index under
    ``index.lock``: this runs per request in the serving checkout, so a lock
    here makes Update's ``git pull`` fail, and a timeout kill leaves it stale.
    """
    try:
        out = subprocess.check_output(
            ["git", "--no-optional-locks", *args],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("utf-8").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError, UnicodeDecodeError):
        return None


def _git(args: list[str]) -> str:
    """Stripped stdout of a git command; empty on failure (use ``_git_read`` to tell them apart)."""
    return _git_read(args) or ""


def _is_dirty() -> bool | None:
    """Whether the working tree has uncommitted changes; None when git could not say.

    A failed or timed-out ``git status`` is unknown, never a clean tree.
    """
    status = _git_read(["status", "--porcelain"])
    return None if status is None else bool(status)


def _capture_backend_build_info() -> dict:
    """Run git in repo root, return a build-info-shaped dict. Never raises.

    In an app bundle the backend shipped with the frontend from one build, so
    the bundled build-info.json is the backend's identity too.
    """
    now = datetime.now(timezone.utc)
    build_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    label = now.strftime("%Y.%m.%d.%H%M")
    if _APP_BUNDLE is not None:
        return {**_read_frontend_build_info(), "build_iso": build_iso}

    full_hash = _git(["rev-parse", "HEAD"]) or "unknown"
    short_hash = _git(["rev-parse", "--short=7", "HEAD"]) or "unknown"
    commit_iso = _git(["log", "-1", "--format=%cI", "HEAD"]) or build_iso
    commit_subject = _git(["log", "-1", "--format=%s", "HEAD"]) or "(git unavailable)"
    dirty = _is_dirty()

    return {
        "label": label,
        "short_hash": short_hash,
        "full_hash": full_hash,
        "commit_iso": commit_iso,
        "commit_subject": commit_subject,
        "dirty": dirty,
        "build_iso": build_iso,
    }


def _placeholder_frontend_info() -> dict:
    return {
        "label": "unknown",
        "short_hash": "unknown",
        "full_hash": "unknown",
        "commit_iso": "",
        "commit_subject": "(frontend not built)",
        "dirty": None,
        "build_iso": "",
        "app": {"version": "unknown", "shell_protocol": 1},
    }


def _read_frontend_build_info() -> dict:
    """Read frontend/dist/build-info.json. Returns placeholder if missing/unreadable.

    Re-read per request so an in-place rebuild is reflected without a server
    restart. File is small (~300B) and traffic is low — cost is negligible.
    """
    path = FRONTEND_DIR / "build-info.json"
    if not path.exists():
        return _placeholder_frontend_info()
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Failed to read %s: %s", path, e)
        return _placeholder_frontend_info()


# Captured once at module import (= server start). Cheap, no per-request cost.
_BACKEND_BUILD: dict = _capture_backend_build_info()
_BACKEND_BUILD["start_iso"] = _BACKEND_BUILD["build_iso"]  # alias for clarity


def _current_head_info() -> dict:
    """Re-read git HEAD per request with five blocking git subprocesses.

    Distinct from `_BACKEND_BUILD` (frozen at module import = process
    start). When this differs from the running process's hash, new
    commits have landed since the backend last restarted.
    """
    return {
        "short_hash": _git(["rev-parse", "--short=7", "HEAD"]) or "unknown",
        "full_hash": _git(["rev-parse", "HEAD"]) or "unknown",
        "commit_iso": _git(["log", "-1", "--format=%cI", "HEAD"]) or "",
        "commit_subject": _git(["log", "-1", "--format=%s", "HEAD"]) or "(git unavailable)",
        "dirty": _is_dirty(),
    }


def _version_payload() -> dict:
    """Build the version payload; git and file reads block, so callers offload it.

    ``generation`` is the installed service's identity (the launcher's
    ``CORSIA_SERVICE_GENERATION``), which an app update proves against the
    bundle it swapped in; an app bundle has no checkout head.
    """
    backend = dict(_BACKEND_BUILD)
    backend["current_head"] = None if _APP_BUNDLE is not None else _current_head_info()
    backend["generation"] = os.environ.get("CORSIA_SERVICE_GENERATION")
    frontend = _read_frontend_build_info()
    app = frontend.get("app")
    if not isinstance(app, dict):
        app = {"version": "unknown", "shell_protocol": 1}
    return {
        "frontend": frontend,
        "backend": backend,
        "app": app,
    }


@router.get("/version")
async def get_version():
    """Public. Returns server-side view of frontend + backend build identity."""
    return await asyncio.to_thread(_version_payload)
