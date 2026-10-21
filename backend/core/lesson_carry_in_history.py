"""Immutable carry-in target and case provenance helpers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def case_sha(case: dict[str, Any] | None) -> str | None:
    """Version the exact executable case used for a result without storing its text."""
    if case is None:
        return None
    payload = json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def recorded_target_blob(workspace: Path, fact) -> tuple[bytes | None, str | None]:
    """Read immutable target bytes from guidance history, never live disk."""
    from infra.guidance_git import GitFault, _git_bytes, workspace_repository

    repo, reason = workspace_repository(workspace)
    if repo is None:
        return None, reason or "guidance history unavailable"
    try:
        return _git_bytes(repo, "show", f"{fact.target_commit}:{fact.target_path}"), None
    except (GitFault, ValueError):
        return None, "recorded_target_unavailable"


def target_snapshot(workspace: Path, fact) -> tuple[str | None, str | None]:
    """Return a target hash only while disk equals the recorded commit blob."""
    recorded, reason = recorded_target_blob(workspace, fact)
    if recorded is None:
        return None, reason
    try:
        on_disk = (workspace / fact.target_path).read_bytes()
    except OSError:
        return None, "recorded_target_unavailable"
    if on_disk != recorded:
        return None, "target_changed_after_commit"
    return hashlib.sha256(recorded).hexdigest(), None
