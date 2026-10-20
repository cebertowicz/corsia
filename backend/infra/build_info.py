"""Read the build identity stamped into the frontend or source checkout."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from infra.config import FRONTEND_DIR
from infra.platform_paths import app_bundle_root

log = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def placeholder_frontend_info() -> dict:
    """Return the frontend build-info shape when its stamp cannot be read."""
    return {
        "label": "unknown",
        "short_hash": "unknown",
        "full_hash": "unknown",
        "commit_iso": "",
        "commit_subject": "(frontend not built)",
        "dirty": None,
        "build_iso": "",
        "app": {"version": None},
    }


def frontend_build_info() -> dict:
    """Read the frontend build stamp, returning a safe placeholder on failure."""
    path = FRONTEND_DIR / "build-info.json"
    if not path.exists():
        return placeholder_frontend_info()
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read %s: %s", path, exc)
        return placeholder_frontend_info()


def _git_read(args: list[str]) -> str | None:
    """Read source-checkout git metadata without updating its index."""
    try:
        output = subprocess.check_output(
            ["git", "--no-optional-locks", *args],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return output.decode("utf-8").strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError, UnicodeDecodeError):
        return None


def app_version(frontend: dict | None = None) -> str | None:
    """Return the stamped app version, or the source checkout's nearest release."""
    info = frontend_build_info() if frontend is None else frontend
    app = info.get("app") if isinstance(info, dict) else None
    stamped = app.get("version") if isinstance(app, dict) else None
    if isinstance(stamped, str) and stamped:
        return stamped
    if app_bundle_root() is not None:
        return None
    described = _git_read(["describe", "--tags", "--match", "v[0-9]*"])
    return described.removeprefix("v") if described else None
