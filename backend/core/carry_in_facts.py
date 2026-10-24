"""C1 carry-in facts: which kept lesson cases a committed guidance save puts to the test.

A trial lesson's case names the file its rule changes. When a guidance save
commits that file, ``core.carry_in_scan`` appends one ``lesson.carry_in.pending``
outcome event per matching, currently eligible case, within the save's budget.
That append-only sequence is the whole work list: claim, result, retirement,
coverage and waiting facts follow it under the same identity, and nothing
mirrors it in another table. Facts carry ids, hashes, state and counts only,
never case text or transcripts.

The drain (``infra.carry_in_drain``) reads these facts; ``infra.guidance_git``
owns the commits they point at.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PENDING = "lesson.carry_in.pending"
CLAIMED = "lesson.carry_in.claimed"
RESULT = "lesson.carry_in.result"
RETIRED = "lesson.carry_in.retired"
COVERAGE = "lesson.carry_in.coverage"
WAITING = "lesson.carry_in.waiting"
TERMINAL_EVENTS = (RESULT, RETIRED, COVERAGE)

#: Why a pending case left the work list without a scored result; its file owner acts.
RETIRE_REASONS = (
    "lesson_unreadable", "lesson_retired", "version_changed", "case_changed",
    "target_moved", "owner_missing", "manifest_mismatch", "not_mechanically_executable",
    "target_unavailable",
)
#: Why a claimed case ended without enough completed samples: the eval route's own
#: failure kinds (``eval.endpoint.EVAL_FAILURES``), a restart, lost capacity, or
#: ``error``, a fault in this check's own code.
COVERAGE_REASONS = (
    "admission", "handoff", "provider", "timeout", "interrupted", "no_ready_local_model", "error",
)
#: Why the oldest case is still pending; it stays the oldest and runs on a later wake.
WAITING_REASONS = ("no_ready_local_model", "history_unavailable")
VERDICTS = ("passed", "failed", "not_discriminating")
_TARGET_KEYS = ("target_file", "new_target_path")
_ACTIVE_STATES = ("watch", "trial", "promoted")


@dataclass(frozen=True)
class CarryInFact:
    """One case put to the test against one committed revision of its target."""

    lesson_id: str
    behavioral_sha256: str
    case_sha256: str
    owner_thread_id: str
    lesson_path: str
    target_path: str
    target_commit: str

    @property
    def identity(self) -> str:
        payload = "\0".join((
            self.lesson_id, self.behavioral_sha256, self.target_path,
            self.target_commit, self.case_sha256,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def subject_key(self) -> str:
        return f"c1:{self.identity}"

    def dedupe_key(self, state: str) -> str:
        return f"lesson.carry_in:{self.identity}:{state}"

    def attributes(self) -> dict[str, Any]:
        return {
            "identity": self.identity, "lesson_id": self.lesson_id,
            "behavioral_sha256": self.behavioral_sha256, "case_sha256": self.case_sha256,
            "owner_thread_id": self.owner_thread_id, "lesson_path": self.lesson_path,
            "target_path": self.target_path, "target_commit": self.target_commit,
        }

    @classmethod
    def from_attributes(cls, attrs: dict[str, Any]) -> CarryInFact | None:
        try:
            fact = cls(**{key: str(attrs[key]) for key in (
                "lesson_id", "behavioral_sha256", "case_sha256", "owner_thread_id",
                "lesson_path", "target_path", "target_commit",
            )})
        except (KeyError, TypeError):
            return None
        return fact if fact.identity == attrs.get("identity") else None


@dataclass(frozen=True)
class EligibleCase:
    """A pending fact's case as its lesson file holds it now."""

    case: dict[str, Any]
    actions: dict[str, Any] | None


def commit_parent(workspace: Path, sha: str) -> str | None:
    """The first parent of one recorded guidance commit; None for a root commit."""
    from infra.guidance_history_paths import history_git

    out = history_git(workspace, "rev-parse", "--verify", "-q", f"{sha}^", ok=(0, 1))
    return out.decode().strip() or None


def committed_bytes(workspace: Path, commit: str, rel: str) -> bytes | None:
    """One workspace-relative path's exact bytes at a recorded commit; None when it has none."""
    from infra.guidance_history_paths import history_git

    oid = history_git(workspace, "rev-parse", "--verify", "-q", f"{commit}:{rel}", ok=(0, 1))
    return history_git(workspace, "cat-file", "blob", oid.decode().strip()) if oid.strip() else None


def case_sha256(case: dict[str, Any]) -> str:
    payload = json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_lesson(path: Path):
    from core.lesson_files import read_trial

    try:
        return read_trial(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def owner_tray(db: sqlite3.Connection, owner: str, trays: dict[str, Path | None] | None = None) -> Path | None:
    """The owner's tray; ``trays`` memoizes it across one scan's lessons."""
    from core.placement_answer import thread_place

    if trays is not None and owner in trays:
        return trays[owner]
    tray = None
    if owner and db.execute("SELECT 1 FROM threads WHERE id=?", (owner,)).fetchone() is not None:
        tray = thread_place(db, owner).tray
    if trays is not None:
        trays[owner] = tray
    return tray


def canonical_target(tray: Path, workspace: Path, target: str, resolved_workspace: Path | None = None) -> str | None:
    """A case target as a workspace-relative path, the way placement receipts resolve one.

    The owner's scope root wins over the workspace root; a target outside the
    workspace has no canonical form. ``resolved_workspace`` spares a scan one
    resolve per lesson.
    """
    given = Path(target).expanduser()
    roots = [tray.parent.parent, workspace]
    candidates = [given] if given.is_absolute() else [root / given for root in roots]
    resolved_workspace = resolved_workspace or workspace.resolve()
    fallback = None
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_workspace):
            continue
        rel = resolved.relative_to(resolved_workspace).as_posix()
        if resolved.is_file():
            return rel
        fallback = fallback or rel
    return fallback


def _lesson_case(db, workspace: Path, lesson_path: str, trays: dict[str, Path | None] | None = None):
    """``(trial, target rel, case)`` or a retirement reason."""
    path = workspace / lesson_path
    trial = _read_lesson(path)
    if trial is None:
        return "lesson_unreadable"
    if trial.lesson.state not in _ACTIVE_STATES:
        return "lesson_retired"
    tray = owner_tray(db, trial.lesson.owner, trays)
    if tray is None or path.resolve().parent.parent != (tray / "skills").resolve():
        return "owner_missing"
    case = trial.lesson.case_metadata.get("case")
    target = next((trial.lesson.case_metadata[key] for key in _TARGET_KEYS
                   if trial.lesson.case_metadata.get(key)), None)
    if not isinstance(case, dict) or not target:
        return "case_changed"
    rel = canonical_target(tray, workspace, str(target))
    if rel is None:
        return "target_moved"
    return trial, rel, case


def eligible_case(db: sqlite3.Connection, workspace: Path, fact: CarryInFact) -> EligibleCase | str:
    """The fact's case if its lesson still holds it unchanged, else the retirement reason."""
    found = _lesson_case(db, workspace, fact.lesson_path)
    if isinstance(found, str):
        return found
    trial, rel, case = found
    if trial.lesson.id != fact.lesson_id or trial.lesson.owner != fact.owner_thread_id:
        return "lesson_unreadable"
    if trial.behavioral_sha256 != fact.behavioral_sha256:
        return "version_changed"
    if case_sha256(case) != fact.case_sha256:
        return "case_changed"
    if rel != fact.target_path:
        return "target_moved"
    return EligibleCase(case=case, actions=case.get("actions"))


def committed_cases(
    db: sqlite3.Connection, workspace: Path, saved: set[str], commit: str, lessons: list[str],
    trays: dict[str, Path | None] | None = None,
) -> list[CarryInFact]:
    """Every eligible case among ``lessons`` whose canonical target is one of the saved paths.

    Reads each lesson file once and each distinct owner's place once (``trays``
    carries those across one scan).
    """
    facts = []
    trays = {} if trays is None else trays
    for lesson_path in lessons:
        found = _lesson_case(db, workspace, lesson_path, trays)
        if isinstance(found, str) or found[1] not in saved:
            continue
        trial, rel, case = found
        facts.append(CarryInFact(
            lesson_id=trial.lesson.id, behavioral_sha256=trial.behavioral_sha256,
            case_sha256=case_sha256(case), owner_thread_id=trial.lesson.owner,
            lesson_path=lesson_path, target_path=rel, target_commit=commit,
        ))
    return facts


def _record(db, fact: CarryInFact, event: str, key: str, ts: str, **extra: Any) -> bool:
    from infra.outcome_events import record_outcome_event

    return record_outcome_event(
        db, dedupe_key=fact.dedupe_key(key), event_name=event, source="corsia",
        provenance="instrumented", ts=ts, subject_key=fact.subject_key,
        agent_thread_id=fact.owner_thread_id,
        attributes={"identity": fact.identity, **extra} if event != PENDING else fact.attributes(),
    ).inserted


def record_pending(db: sqlite3.Connection, fact: CarryInFact, ts: str) -> bool:
    """Append the pending fact unless this identity already has any fact: one read, one insert."""
    keys = tuple(fact.dedupe_key(state) for state in ("pending", "claimed", "terminal"))
    if db.execute("SELECT 1 FROM outcome_events WHERE dedupe_key IN (?,?,?) LIMIT 1", keys).fetchone():
        return False
    return _record(db, fact, PENDING, "pending", ts)


def record_claim(db: sqlite3.Connection, fact: CarryInFact, ts: str, model: str, boot: str) -> bool:
    """The claim names the process boot that runs it, so a later wake can tell a restart from a fault."""
    return _record(db, fact, CLAIMED, "claimed", ts, model=model, boot=boot)


def record_waiting(db: sqlite3.Connection, fact: CarryInFact, ts: str, reason: str) -> bool:
    return _record(db, fact, WAITING, f"waiting:{reason}", ts, reason=reason)


def record_terminal(
    db: sqlite3.Connection, fact: CarryInFact, ts: str, event: str, **attrs: Any,
) -> bool:
    """One result, retirement or coverage fact; the first terminal for an identity wins."""
    assert event in TERMINAL_EVENTS
    return _record(db, fact, event, "terminal", ts, **attrs)


def claim_boot(db: sqlite3.Connection, fact: CarryInFact) -> str | None:
    row = db.execute(
        "SELECT json_extract(attributes, '$.boot') FROM outcome_events WHERE dedupe_key=?",
        (fact.dedupe_key("claimed"),),
    ).fetchone()
    return row[0] if row else None


def open_facts(db: sqlite3.Connection, *, claimed: bool, limit: int = -1) -> list[CarryInFact]:
    """Pending facts without a terminal, oldest first; ``claimed`` selects claimed ones.

    ``limit`` bounds the read (SQLite's -1 is none); the drain reads one per wake.
    """
    rows = db.execute(
        """SELECT p.attributes FROM outcome_events p
           WHERE p.event_name=? AND p.retracted_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM outcome_events t
                             WHERE t.dedupe_key = 'lesson.carry_in:' ||
                                   json_extract(p.attributes, '$.identity') || ':terminal')
             AND EXISTS (SELECT 1 FROM outcome_events c
                         WHERE c.dedupe_key = 'lesson.carry_in:' ||
                               json_extract(p.attributes, '$.identity') || ':claimed') = ?
           ORDER BY p.ts, p.observed_ts, p.id LIMIT ?""",
        (PENDING, int(claimed), limit),
    ).fetchall()
    facts = (CarryInFact.from_attributes(json.loads(row[0])) for row in rows)
    return [fact for fact in facts if fact is not None]


def has_terminal_with_prefix(db: sqlite3.Connection, prefix: str) -> bool:
    return db.execute(
        "SELECT 1 FROM outcome_events WHERE dedupe_key LIKE ? AND dedupe_key LIKE '%:terminal' LIMIT 1",
        (f"lesson.carry_in:{prefix}%",),
    ).fetchone() is not None
