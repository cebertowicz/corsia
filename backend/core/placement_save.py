"""Atomic placement-save lifecycle, separated from placement decision outcomes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def save_placement(
    entry: dict[str, Any], place: Any, decision: Any, relative: str, path: Path,
    writes: dict[Path, str], expected: dict[Path, str | None], operations: list[Any], *,
    settle: Callable[..., None], defer: Callable[..., None], refuse: Callable[..., None],
    author: Callable[[Any], str], message: Callable[..., str], logger: Any,
) -> str | None:
    """Prevalidate then commit placement bytes; failures leave the entry reconsiderable."""
    from core.context_source_paths import append_context_source_change
    from core.placement_proposal import marker
    from infra.config import WORKSPACE
    from infra.guidance_git import GuidanceTarget, find_commit, save_guidance, workspace_repository
    from infra.skill_save_validation import REFUSED, invalid_skill_write

    encoded = {target: text.encode() for target, text in writes.items()}
    if (refusal_result := invalid_skill_write(encoded)) is not None:
        refuse(entry, relative, refusal_result.errors)
        return None
    trailer = marker(entry["thread_id"], entry["entry_id"], entry["revision"])
    repo, _why = workspace_repository(Path(WORKSPACE), create=True)
    if repo is None:
        settle(entry, "unplaced", "history-off", destination=relative)
        return None
    sha = find_commit(repo, trailer, path)
    if sha is not None:
        return sha
    result = save_guidance(
        encoded, workspace=Path(WORKSPACE), target=GuidanceTarget("placement"), expected=expected,
        atomic=True, author=author(place), message=message(entry, decision, relative, trailer, operations),
    )
    if result.outcome in ("deferred", "stale"):
        defer(entry, result.reason, destination=relative)
    elif result.outcome == REFUSED:
        refuse(entry, relative, result.errors)
    elif result.outcome != "written":
        settle(entry, "unplaced", result.reason, destination=relative)
    else:
        try:
            append_context_source_change(None, path=str(path), actor_thread_id=entry["thread_id"])
        except Exception:
            logger.exception("placement context-change notice failed: {}", relative)
        return result.sha
    return None
