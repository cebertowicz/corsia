"""Read recorded file versions from the workspace's existing Git repository."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from infra.guidance_git import _git_bytes, workspace_repository
from infra.guidance_states import GitFault, history_is_off
from infra.guidance_messages import parse_recorded_reason

# The most versions one list returns; ``truncated`` says older ones exist.
HISTORY_CAP = 500
# Consecutive saves by one author this close together show as one version.
GROUP_GAP = timedelta(minutes=10)
_COMMIT = re.compile(r"[0-9a-f]{40}")


def repository(workspace: Path) -> Path | None:
    """The workspace's own repository, or None when it records no history of its own: none yet, inside an
    enclosing repository, or a Git directory that resolves elsewhere — the states a guidance save
    records nothing for. A Git fault raises ``GitFault``, because the next try may succeed."""
    repo, reason = workspace_repository(workspace.resolve())
    if repo is None and not history_is_off(reason):
        raise GitFault(reason or "Workspace history unavailable")
    return repo


def history_off(workspace: Path) -> bool:
    """Whether to stop offering history for this workspace: it records none of its own. A Git fault is
    not history off — the read says history is unavailable, where trying again can help."""
    try:
        return repository(workspace) is None
    except GitFault:
        return False


def history_offered(workspace: Path, document=None) -> bool:
    """Whether an open recorded file offers History: the workspace records history, and the writer's
    ignore gate lets ``document`` (its path and target; a guidance file passes None) in. A Git fault in
    that check offers none, as for an ignored file."""
    from infra.history_ignore import ignored

    try:
        repo = repository(workspace)
        if repo is None or document is None:
            return repo is not None
        path, target = document
        relative = path.resolve(strict=False).relative_to(repo).as_posix()
        return not ignored(repo, [relative], tray=target.registered)
    except (GitFault, ValueError) as exc:
        logger.warning("history check failed for {}: {}", document, exc)
        return document is None


def list_versions(workspace: Path, relative: str) -> tuple[list[dict[str, str | None]], bool]:
    """The newest versions that hold this file's text, and whether older ones exist past the cap."""
    repo = repository(workspace)
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
    return grouped(rows[:HISTORY_CAP]), len(rows) > HISTORY_CAP


def grouped(rows: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    """Newest-first saves shown as versions: a save joins the version just before it when the same
    author made both within ``GROUP_GAP`` and the later one states no reason. The shown version is the
    group's newest save, under the reason that started it. Display only: every commit stays in history."""
    shown: list[dict[str, str | None]] = []
    for row in reversed(rows):
        last = shown[-1] if shown else None
        if (last is not None and row["reason"] is None and row["author"] == last["author"]
                and _at(row) - _at(last) <= GROUP_GAP):
            shown[-1] = {**row, "reason": last["reason"]}
        else:
            shown.append(row)
    return shown[::-1]


def _at(row: dict[str, str | None]) -> datetime:
    return datetime.fromisoformat(str(row["savedAt"]))


def version_bytes(repo: Path, relative: str, commit: str) -> bytes:
    """One commit's bytes for this file in ``repo``, the caller's one :func:`repository` lookup;
    ``ValueError`` when that commit holds no such file."""
    if not _COMMIT.fullmatch(commit):
        raise ValueError("Version unavailable")
    oid = _git_bytes(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}:{relative}", ok=(0, 1)).strip()
    if not oid:
        raise ValueError("Version unavailable")
    return _git_bytes(repo, "cat-file", "blob", oid.decode())


def version_text(repo: Path, relative: str, commit: str) -> str:
    return version_bytes(repo, relative, commit).decode(errors="replace")


def restore_reason(repo: Path, commit: str) -> str:
    """The recorded reason for restoring ``commit`` from ``repo``, with the year and zone so it stays true later and elsewhere."""
    author, _, at = _git_bytes(repo, "log", "-1", "--format=%an%x00%at", commit).decode(errors="replace").strip().partition("\0")
    when = datetime.fromtimestamp(int(at)).astimezone()
    hour = int(when.strftime("%I"))
    return f"Restored {author}’s version of {when:%b} {when.day}, {when.year} at {hour}:{when:%M %p %Z}."
