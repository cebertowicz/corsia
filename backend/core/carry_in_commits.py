"""C1 carry-in saved targets: a written guidance save is matched to trial cases off the request path.

``record_committed_save`` appends one ``lesson.carry_in.saved`` fact per changed
target. The drain later calls ``process_saved_commit``, which discovers the cases
that name that target and appends their pending facts (``core.carry_in_facts``
owns every fact's shape and the rest of the sequence), then closes the saved item.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.carry_in_facts import (
    PENDING, SAVED, SAVED_PROCESSED, CarryInFact, lesson_case, case_sha256, record_pending,
)


@dataclass(frozen=True)
class SavedCommit:
    """One written target that the drain must match against trial lessons."""

    commit: str
    target_path: str

    @property
    def identity(self) -> str:
        return hashlib.sha256(f"{self.commit}\0{self.target_path}".encode("utf-8")).hexdigest()

    @property
    def dedupe_key(self) -> str:
        return f"lesson.carry_in.saved:{self.identity}"

    @property
    def processed_key(self) -> str:
        return f"lesson.carry_in.saved:{self.identity}:processed"

    def attributes(self) -> dict[str, str]:
        return {"identity": self.identity, "commit": self.commit, "target_path": self.target_path}


def _scope_roots(db: sqlite3.Connection, workspace: Path) -> list[Path]:
    """The workspace root and every project root, as the loader resolves them.

    Thread trays live only at ``<scope root>/.threads/<id>``; a repository
    checked out under a project carries its own ``.threads`` that CORSIA never
    loads, so the search never recurses below a scope root.
    """
    from core.workspace_layout import resolve_project_root, resolve_workspace_root

    roots = [resolve_workspace_root(workspace)]
    for (project_id,) in db.execute("SELECT id FROM projects ORDER BY id").fetchall():
        try:
            root = resolve_project_root(workspace, str(project_id), db=db)
        except (OSError, RuntimeError, ValueError):
            continue
        if root is not None:
            roots.append(root.resolve(strict=False))
    return list(dict.fromkeys(roots))


def _baseline_from(db: sqlite3.Connection, lesson_id: str, target_path: str) -> str | None:
    """The first target commit this lesson was checked against, if any."""
    row = db.execute(
        """SELECT COALESCE(json_extract(attributes, '$.baseline_from'),
                           json_extract(attributes, '$.target_commit'))
           FROM outcome_events
           WHERE event_name=? AND retracted_at IS NULL
             AND json_extract(attributes, '$.lesson_id')=?
             AND json_extract(attributes, '$.target_path')=?
           ORDER BY ts, observed_ts, id LIMIT 1""",
        (PENDING, lesson_id, target_path),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def committed_cases(db: sqlite3.Connection, workspace: Path, saved: SavedCommit) -> list[CarryInFact]:
    """Current eligible cases whose canonical target is one saved target.

    This is deliberately the one parser path: ``lesson_case`` uses
    ``lesson_files.read_trial``. Bad lesson files and unresolvable targets are
    non-matches, never a failure of this saved work item. A lesson already
    checked against this target keeps its first commit's pre-lesson baseline.
    """
    root = workspace.resolve()
    facts = []
    for scope_root in _scope_roots(db, workspace):
        if not scope_root.is_relative_to(root):
            continue  # case targets and lesson paths are workspace-relative
        for path in sorted(scope_root.glob(".threads/*/skills/*/SKILL.md")):
            lesson_path = path.relative_to(root).as_posix()
            found = lesson_case(db, workspace, lesson_path)
            if isinstance(found, str) or found[1] != saved.target_path:
                continue
            trial, rel, case = found
            facts.append(CarryInFact(
                lesson_id=trial.lesson.id, behavioral_sha256=trial.behavioral_sha256,
                case_sha256=case_sha256(case), owner_thread_id=trial.lesson.owner,
                lesson_path=lesson_path, target_path=rel, target_commit=saved.commit,
                baseline_from=_baseline_from(db, trial.lesson.id, rel) or saved.commit,
            ))
    return facts


def record_committed_save(workspace: Path, paths: list[Path], commit: str) -> None:
    """Record written targets only; lesson discovery belongs to the drain, not the save."""
    from contextlib import closing

    from infra.database import get_db, write_transaction
    from infra.outcome_events import record_outcome_event
    from infra.time import utc_now_iso

    root = workspace.resolve()
    saved = {
        SavedCommit(commit, path.resolve(strict=False).relative_to(root).as_posix())
        for path in paths
        if path.resolve(strict=False).is_relative_to(root)
    }
    if not saved:
        return
    with closing(get_db()) as db, write_transaction(db, scope="carry_in_saved") as tx:
        for saved_commit in sorted(saved, key=lambda item: item.target_path):
            record_outcome_event(
                tx, dedupe_key=saved_commit.dedupe_key, event_name=SAVED, source="corsia",
                provenance="instrumented", ts=utc_now_iso(), subject_key=f"c1save:{saved_commit.identity}",
                attributes=saved_commit.attributes(),
            )


def oldest_saved_commit(db: sqlite3.Connection) -> SavedCommit | None:
    """The oldest saved target not yet matched; one drain wake handles one."""
    row = db.execute(
        """SELECT json_extract(s.attributes, '$.commit'), json_extract(s.attributes, '$.target_path')
           FROM outcome_events s
           WHERE s.event_name=? AND s.retracted_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM outcome_events p
                             WHERE p.dedupe_key = 'lesson.carry_in.saved:' ||
                                   json_extract(s.attributes, '$.identity') || ':processed')
           ORDER BY s.ts, s.observed_ts, s.id LIMIT 1""",
        (SAVED,),
    ).fetchone()
    return SavedCommit(str(row[0]), str(row[1])) if row and row[0] and row[1] else None


def process_saved_commit(db: sqlite3.Connection, workspace: Path, saved: SavedCommit, ts: str) -> int:
    """Append matching pending facts and close one saved work item atomically."""
    from infra.database import write_transaction
    from infra.outcome_events import record_outcome_event

    matches = committed_cases(db, workspace, saved)
    with write_transaction(db, scope="carry_in_match_saved") as tx:
        inserted = sum(record_pending(tx, fact, ts) for fact in matches)
        record_outcome_event(
            tx, dedupe_key=saved.processed_key, event_name=SAVED_PROCESSED, source="corsia",
            provenance="instrumented", ts=ts, subject_key=f"c1save:{saved.identity}",
            attributes={"identity": saved.identity, "pending": inserted},
        )
    return inserted


def queue_saved_commit(workspace: Path, paths: list[Path], commit: str) -> str | None:
    """Record a landed save's targets and wake the drain; a warning line when the recorder faults.

    The commit has landed, so a recorder fault must not unsay it.
    """
    from loguru import logger

    from infra.carry_in_drain import request_drain

    try:
        record_committed_save(workspace, paths, commit)
        request_drain(workspace)
    except Exception as exc:
        logger.opt(exception=True).warning("carry-in save not recorded for {}", commit)
        return f"Warning: saved, but its skill re-test was not queued ({type(exc).__name__})."
    return None
