"""C1 carry-in facts: written saves become bounded work off the request path.

A trial lesson's case names the file its rule changes. A written guidance save
first appends one ``lesson.carry_in.saved`` outcome event per changed target.
The drain discovers matching cases from that saved fact off the request path,
then appends pending facts. That append-only sequence is the whole work list:
claim, result, retirement, coverage and waiting facts follow it under the same
identity, and nothing mirrors it in another table. Facts carry ids, hashes,
state and counts only, never case text or transcripts.

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
SAVED = "lesson.carry_in.saved"
SAVED_PROCESSED = "lesson.carry_in.saved_processed"
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
    "target_unavailable", "not_offered",
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
    #: The first target commit this lesson was checked against; its parent is the
    #: pre-lesson baseline every later commit keeps. Empty: ``target_commit``.
    baseline_from: str = ""

    @property
    def baseline_commit(self) -> str:
        """The commit whose first parent holds the pre-lesson target."""
        return self.baseline_from or self.target_commit

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
            "baseline_from": self.baseline_commit,
        }

    @classmethod
    def from_attributes(cls, attrs: dict[str, Any]) -> CarryInFact | None:
        try:
            fact = cls(**{key: str(attrs[key]) for key in (
                "lesson_id", "behavioral_sha256", "case_sha256", "owner_thread_id",
                "lesson_path", "target_path", "target_commit",
            )}, baseline_from=str(attrs.get("baseline_from") or ""))
        except (KeyError, TypeError):
            return None
        return fact if fact.identity == attrs.get("identity") else None


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


#: A sample's side: ``b`` tests the pre-lesson baseline, ``c`` the saved candidate.
SIDES = ("b", "c")


def side_bytes(workspace: Path, fact: CarryInFact, side: str) -> bytes | None:
    """The target exactly as one side tests it; None when that revision has no such file.

    The baseline is the parent of the first commit this lesson was checked
    against, so a later edit is still measured against the pre-lesson file.
    A Git fault raises ``GitFault``.
    """
    if side == "c":
        return committed_bytes(workspace, fact.target_commit, fact.target_path)
    if side != "b":
        raise ValueError(f"unknown carry-in side {side!r}")
    parent = commit_parent(workspace, fact.baseline_commit)
    return committed_bytes(workspace, parent, fact.target_path) if parent else None


def claimed_fact(db: sqlite3.Connection, identity: str) -> CarryInFact:
    """The claimed, unsettled fact behind one eval thread; raises ``LookupError`` otherwise."""
    row = db.execute(
        "SELECT attributes FROM outcome_events WHERE dedupe_key=?",
        (f"lesson.carry_in:{identity}:pending",),
    ).fetchone()
    fact = CarryInFact.from_attributes(json.loads(row[0])) if row else None
    if fact is None or fact.identity != identity:
        raise LookupError("no pending carry-in case has this identity")
    keys = (fact.dedupe_key("claimed"), fact.dedupe_key("terminal"))
    found = {r[0] for r in db.execute(
        "SELECT dedupe_key FROM outcome_events WHERE dedupe_key IN (?,?)", keys,
    ).fetchall()}
    if keys[0] not in found or keys[1] in found:
        raise LookupError("the carry-in case is not claimed and unsettled")
    return fact


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
    try:
        rel = canonical_target(tray, workspace, str(target))
    except (OSError, RuntimeError, ValueError):
        return "target_moved"
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
    from core.lesson_index import trial_refusal

    # Only a trial the skills index would offer can reach a sample's context.
    if trial_refusal(trial.lesson, trial.behavioral_sha256, db=db) is not None:
        return "not_offered"
    return EligibleCase(case=case, actions=case.get("actions"))


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

    This is deliberately the one parser path: ``_lesson_case`` uses
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
            found = _lesson_case(db, workspace, lesson_path)
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
