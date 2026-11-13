"""What a rule-started worker reads, and the brief it starts with (design §8.1).

A worker reads two things only: one directory in its rule owner's staging folder, where
the workflow that started it puts what it works from (a request may carry none), and the start's
own directory, ``<owner thread root>/workers/<decision id>``. The start's directory is always
there and always empty: ``untrusted`` confinement needs at least one read path
(``tools/delegate_task.py``), so a worker started without staging, such as the movement digest
worker, still has one and reads nothing. Both are read paths of the worker, never its working
directory or an ancestor of it, so a ``.claude/``, ``CLAUDE.md`` or hooks inside them are never
loaded. The start's directory is removed when the start settles. A staging directory outside the
staging folder refuses the start with :class:`StartRefused`, whose text the owner reads.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The request facts a ``start_worker`` rule's worker reads (named in the tool's description).
STAGING_FACT = "staging_path"
HEAD_SHA_FACT = "head_sha"


class StartRefused(Exception):
    """Why a start can't run; its text is what the owner is told."""


class StartWaiting(StartRefused):
    """A refusal the next pass may not repeat: the start goes back to the queue, and its text is
    what the owner is told if it is still refused when the wait ends."""


def owner_root(owner: str, project_id: str | None, db: sqlite3.Connection) -> Path:
    """The rule owner's thread root, the one its own confined children resolve."""
    from core.workspace_layout import resolve_thread_root
    from subagents.cc_runtime_helpers import _main_working_dir

    return resolve_thread_root(_main_working_dir(project_id, db), owner)


def start_dir(root: Path, decision_id: int) -> Path:
    """The start's empty directory: the read path of a worker that has no staging."""
    return root / "workers" / str(decision_id)


def remove_start_dir(root: Path, decision_id: int) -> None:
    shutil.rmtree(start_dir(root, decision_id), ignore_errors=True)


def remove_staging_dir(root: Path, value: Any) -> None:
    """Remove a staged input directory, never the staging root itself."""
    if not value:
        return
    folder = (root / "staging").resolve()
    raw = Path(str(value))
    path = (raw if raw.is_absolute() else folder / raw).resolve()
    if path != folder and folder in path.parents:
        shutil.rmtree(path, ignore_errors=True)


def remove_unclaimed_staging(owner: str, project_id: str | None, event_id: str,
                             value: Any, db: sqlite3.Connection) -> None:
    """Remove staged input only when no worker start claimed the event."""
    claimed = db.execute(
        "SELECT 1 FROM event_route_decisions WHERE event_id=? AND request_identity IS NOT NULL LIMIT 1",
        (event_id,),
    ).fetchone()
    if not claimed:
        remove_staging_dir(owner_root(owner, project_id, db), value)


def staging_dir(root: Path, value: Any) -> str | None:
    """The request's staging directory, only inside the owner's own staging folder."""
    if value is None or value == "":
        return None
    folder = (root / "staging").resolve()
    raw = Path(str(value))
    path = (raw if raw.is_absolute() else folder / raw).resolve()
    if path != folder and folder not in path.parents:
        raise StartRefused(f"refused: {STAGING_FACT} {value} is outside your staging folder {folder}")
    if not path.is_dir():
        raise StartRefused(f"refused: {STAGING_FACT} {value} is not a directory in your staging folder")
    return str(path)


def skill_body(path: Path) -> str:
    """A workflow skill's instructions, without its frontmatter."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def brief(workflow: str, body: str, schema: Mapping[str, Any], read_paths: list[str],
          event: Mapping[str, Any]) -> str:
    """The worker's one task: its workflow, what it may read, the event and the answer's shape."""
    return "\n\n".join((
        f"You are a read-only worker running the workflow {workflow}. You can read only "
        f"these directories: {', '.join(read_paths)}. You cannot write, run commands or "
        "reach the network.",
        f"The event that started you: {event.get('type')}, with these facts:\n"
        f"{json.dumps(event.get('facts') or {}, ensure_ascii=False, indent=1, default=str)}",
        body,
        "End with one final message whose answer is a JSON value matching this schema, "
        "alone or as its last fenced json block:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=1)}",
    ))
