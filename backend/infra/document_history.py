"""Read recorded file versions from the workspace's existing Git repository."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from infra.guidance_git import GitFault, NoRepository, _git_bytes, workspace_repository
from infra.guidance_messages import parse_recorded_reason

# The most versions one list returns; ``truncated`` says older ones exist.
HISTORY_CAP = 500
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _repository(workspace: Path) -> Path | None:
    repo, reason = workspace_repository(workspace.resolve())
    if repo is None and not isinstance(reason, NoRepository):
        raise GitFault(reason or "Workspace history unavailable")
    return repo


def list_versions(workspace: Path, relative: str) -> tuple[list[dict[str, str | None]], bool]:
    """The newest versions that hold this file's text, and whether older ones exist past the cap."""
    repo = _repository(workspace)
    if repo is None or not _git_bytes(repo, "rev-parse", "--verify", "--quiet", "HEAD", ok=(0, 1)).strip():
        return [], False
    raw = _git_bytes(
        repo, "log", "-z", f"--max-count={HISTORY_CAP + 1}", "--diff-filter=d",
        "--format=%H%x00%an%x00%aI%x00%B", "--", f":(literal){relative}",
    ).decode(errors="replace")
    fields = raw.split("\0")
    rows = []
    for index in range(0, len(fields) - 3, 4):
        commit, author, saved_at, message = fields[index:index + 4]
        rows.append({"id": commit, "author": author, "savedAt": saved_at, "reason": parse_recorded_reason(message)})
    return rows[:HISTORY_CAP], len(rows) > HISTORY_CAP


def version_bytes(workspace: Path, relative: str, commit: str) -> bytes:
    """One commit's bytes for this file; ``ValueError`` when that commit holds no such file."""
    if not _COMMIT.fullmatch(commit) or (repo := _repository(workspace)) is None:
        raise ValueError("Version unavailable")
    oid = _git_bytes(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}:{relative}", ok=(0, 1)).strip()
    if not oid:
        raise ValueError("Version unavailable")
    return _git_bytes(repo, "cat-file", "blob", oid.decode())


def version_text(workspace: Path, relative: str, commit: str) -> str:
    return version_bytes(workspace, relative, commit).decode(errors="replace")


def restore_reason(workspace: Path, commit: str) -> str:
    """The recorded reason for restoring ``commit``, with the year and zone so it stays true later and elsewhere."""
    repo = _repository(workspace)
    if repo is None:
        raise ValueError("Version unavailable")
    author, _, at = _git_bytes(repo, "log", "-1", "--format=%an%x00%at", commit).decode(errors="replace").strip().partition("\0")
    when = datetime.fromtimestamp(int(at)).astimezone()
    hour = int(when.strftime("%I"))
    return f"Restored {author}’s version of {when:%b} {when.day}, {when.year} at {hour}:{when:%M %p %Z}."
