"""C1 save-path scan: which lessons a committed guidance save puts to the test, within budget.

``record_committed_save`` runs after a guidance commit lands. It does a bounded
amount of work on the save request (the C1 contract's scale budget): one
``git grep`` for lessons that spell a target key, each simple target resolved
exactly as the matcher resolves it, then at most ``SAVE_CASE_BUDGET`` candidates
turned into ``lesson.carry_in.pending`` facts (``core.carry_in_facts``). A scan that would exceed the budget, or whose recorder
failed, appends one ``lesson.carry_in.scan_owed`` fact per saved path instead:
the drain (``infra.carry_in_drain``) runs that scan off the request path and
closes it with ``lesson.carry_in.scanned``. Both live in the same append-only
``outcome_events`` sequence and never copy a pending fact.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from core import carry_in_facts as facts

SCAN_OWED = "lesson.carry_in.scan_owed"
SCANNED = "lesson.carry_in.scanned"
#: Why a save's lesson scan moved off its request path.
SCAN_REASONS = ("over_budget", "failed")
#: Candidate lessons a save resolves inline (C1 contract, scale budget); more defer whole.
SAVE_CASE_BUDGET = 8

SCAN_RETRYING = (
    "Saved. CORSIA couldn't yet re-test the skills that apply to this file against this change; "
    "it will try again in the background."
)
SCAN_LOST = (
    "Saved. CORSIA couldn't re-test the skills that apply to this file against this change "
    "or schedule another try. Save another change to this file to re-test them. Reference: {ref}."
)

#: Distinct lesson owners a save looks up inline (each a few indexed reads); more defer whole.
SAVE_OWNER_BUDGET = 16

_LESSON_TRAYS = ":(glob)**/.threads/*/skills/*/SKILL.md"
# Every line that spells a target or owner key in any YAML form, plus the line after it (-A1).
_KEY_LINES = r"target_file|new_target_path|owner[\"']?[[:space:]]*:"  # POSIX ERE: git grep has no \s
_TARGET_KEY = re.compile(r"target_file|new_target_path")
_OWNER_KEY = re.compile(r"owner[\"']?\s*:")
_OWNER = re.compile("owner")
_SIMPLE = re.compile(r"(?P<indent> *)(?P<key>target_file|new_target_path|owner): +(?P<value>\S(?:.*\S)?) *")
_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
_PLAIN_BREAK = re.compile(r"[ \t]#|:[ \t]|:$")  # a comment or a mapping indicator ends a plain scalar


@dataclass(frozen=True)
class LessonKeys:
    """One lesson file's owner and target text, or None where plain lines cannot prove the value."""

    owner: str | None
    target: str | None


def _scalar(value: str) -> str | None:
    """A one-line YAML string scalar's value: plain or single-quoted; None for any other form."""
    from yaml import ScalarNode
    from yaml.resolver import Resolver

    if value.startswith("'"):
        inner = value[1:-1]
        if len(value) < 2 or not value.endswith("'") or "'" in inner.replace("''", ""):
            return None
        return inner.replace("''", "'")
    if value[0] in _INDICATORS or _PLAIN_BREAK.search(value):
        return None
    if Resolver().resolve(ScalarNode, value, (True, False)) != "tag:yaml.org,2002:str":
        return None
    return value


def _key_value(lines: dict[int, str], numbers: list[int], key: re.Pattern[str]) -> str | None:
    """The value of the one line spelling ``key``, if it is a simple scalar no next line continues."""
    if len(numbers) != 1:
        return None
    match = _SIMPLE.fullmatch(lines[numbers[0]])
    if match is None or not key.fullmatch(match["key"]):
        return None
    following = lines.get(numbers[0] + 1)
    indent = len(match["indent"])
    deeper = following is not None and len(following) > indent and not following[:indent + 1].strip()
    if following is not None and (not following.strip() or deeper):
        return None  # a blank or deeper line may continue the scalar
    return _scalar(match["value"])


def _grep_records(text: str) -> dict[str, dict[int, str]]:
    """``git grep -z -n`` output (``path NUL line NUL content LF``, ``--`` between groups) by file."""
    found: dict[str, dict[int, str]] = {}
    pos = 0
    while pos < len(text):
        if text.startswith("--\n", pos):
            pos += 3
            continue
        name_end = text.index("\0", pos)
        number_end = text.index("\0", name_end + 1)
        line_end = text.find("\n", number_end + 1)
        line_end = len(text) if line_end < 0 else line_end
        found.setdefault(text[pos:name_end], {})[int(text[name_end + 1:number_end])] = (
            text[number_end + 1:line_end].removesuffix("\r")
        )
        pos = line_end + 1
    return found


def lesson_keys(workspace: Path) -> dict[str, LessonKeys]:
    """Tracked lesson files that spell a target key, with what one ``git grep`` proves of each.

    A lesson's target can only be read from a line spelling its key, so a file
    without one has no case. Values come only from one plain or single-quoted
    line; any other form (block, flow, quoted key, alias, escape, continuation or
    a repeated key) leaves the value None, and the lesson is always a candidate.
    """
    from infra.guidance_history_paths import history_git

    out = history_git(
        workspace, "grep", "-z", "-n", "-A1", "-E", _KEY_LINES, "--", _LESSON_TRAYS, ok=(0, 1),
    )
    keys = {}
    for rel, lines in _grep_records(out.decode(errors="surrogateescape")).items():
        targets = [number for number, line in lines.items() if _TARGET_KEY.search(line)]
        if not targets:
            continue
        owners = [number for number, line in lines.items() if _OWNER_KEY.search(line)]
        target = _key_value(lines, targets, _TARGET_KEY)
        owner = _key_value(lines, owners, _OWNER)
        keys[rel] = LessonKeys(
            owner=owner.strip() if owner is not None else None,
            target=" ".join(target.split()) if target is not None else None,  # as lesson_case cleans it
        )
    return keys


def candidate_lessons(
    db: sqlite3.Connection, workspace: Path, saved: set[str], keys: dict[str, LessonKeys],
    *, owner_budget: int | None = None, trays: dict[str, Path | None] | None = None,
) -> list[str] | None:
    """Lesson files whose case may target a saved path; None when the owners exceed ``owner_budget``.

    A proven owner and target resolve through the matcher's own ``owner_tray`` and
    ``canonical_target``, so a symlink, ``..`` or ``~`` resolves exactly as the
    matcher would; an unproven one stays a candidate. Nothing eligible is dropped.
    """
    owners = {k.owner for k in keys.values() if k.owner is not None and k.target is not None}
    if owner_budget is not None and len(owners) > owner_budget:
        return None
    trays = {} if trays is None else trays
    resolved: dict[tuple[Path, str], str | None] = {}
    root = workspace.resolve()
    found = []
    for rel, lesson in keys.items():
        if lesson.owner is None or lesson.target is None:
            found.append(rel)
            continue
        tray = facts.owner_tray(db, lesson.owner, trays)
        if tray is None:
            continue  # the matcher retires an owner without a tray: no case to record
        key = (tray, lesson.target)
        try:
            if key not in resolved:
                resolved[key] = facts.canonical_target(tray, workspace, lesson.target, root)
        except (OSError, RuntimeError, ValueError):  # unresolvable here: let the matcher decide
            found.append(rel)
            continue
        if resolved[key] in saved:
            found.append(rel)
    return found


@dataclass(frozen=True)
class OwedScan:
    """One saved path of one written commit whose lesson scan the drain still owes."""

    commit: str
    target_path: str

    @property
    def identity(self) -> str:
        return hashlib.sha256(f"{self.commit}\0{self.target_path}".encode("utf-8")).hexdigest()

    def dedupe_key(self, state: str) -> str:
        return f"lesson.carry_in.scan:{self.identity}:{state}"


def _record_scan(db, scan: OwedScan, event: str, state: str, ts: str, **attrs: Any) -> bool:
    from infra.outcome_events import record_outcome_event

    return record_outcome_event(
        db, dedupe_key=scan.dedupe_key(state), event_name=event, source="corsia",
        provenance="instrumented", ts=ts, subject_key=f"c1scan:{scan.identity}",
        attributes={"identity": scan.identity, **attrs},
    ).inserted


def record_scan_owed(db: sqlite3.Connection, scan: OwedScan, ts: str, reason: str) -> bool:
    return _record_scan(
        db, scan, SCAN_OWED, "owed", ts,
        commit=scan.commit, target_path=scan.target_path, reason=reason,
    )


def record_scanned(db: sqlite3.Connection, scan: OwedScan, ts: str, pending: int) -> bool:
    return _record_scan(db, scan, SCANNED, "done", ts, pending=pending)


def oldest_owed_scan(db: sqlite3.Connection) -> OwedScan | None:
    """The oldest owed scan not yet closed; the drain runs one per wake."""
    row = db.execute(
        """SELECT json_extract(o.attributes, '$.commit'), json_extract(o.attributes, '$.target_path')
           FROM outcome_events o
           WHERE o.event_name=? AND o.retracted_at IS NULL
             AND NOT EXISTS (SELECT 1 FROM outcome_events d
                             WHERE d.dedupe_key = 'lesson.carry_in.scan:' ||
                                   json_extract(o.attributes, '$.identity') || ':done')
           ORDER BY o.ts, o.observed_ts, o.id LIMIT 1""",
        (SCAN_OWED,),
    ).fetchone()
    return OwedScan(str(row[0]), str(row[1])) if row and row[0] and row[1] else None


def record_cases(
    db: sqlite3.Connection, workspace: Path, saved: set[str], commit: str, lessons: list[str],
    *, closes: OwedScan | None = None, trays: dict[str, Path | None] | None = None,
) -> int:
    """Resolve the cases, then append their pending facts (and close ``closes``) in one short write."""
    from infra.database import write_transaction
    from infra.time import utc_now_iso

    found = facts.committed_cases(db, workspace, saved, commit, lessons, trays)  # computed outside the write
    if not found and closes is None:
        return 0
    now = utc_now_iso()
    with write_transaction(db, scope="carry_in_pending") as tx:
        inserted = sum(facts.record_pending(tx, fact, now) for fact in found)
        if closes is not None:
            record_scanned(tx, closes, now, inserted)
    return inserted


def record_owed_scan(db: sqlite3.Connection, workspace: Path, scan: OwedScan, keys: dict[str, LessonKeys]) -> int:
    """Run one owed scan off the request path, with no owner budget, and close it."""
    saved = {scan.target_path}
    trays: dict[str, Path | None] = {}
    lessons = candidate_lessons(db, workspace, saved, keys, trays=trays) or []
    return record_cases(db, workspace, saved, scan.commit, lessons, closes=scan, trays=trays)


def _owe_scans(saved: set[str], commit: str, reason: str) -> None:
    from contextlib import closing

    from infra.database import get_db, write_transaction
    from infra.time import utc_now_iso

    now = utc_now_iso()
    with closing(get_db()) as db, write_transaction(db, scope="carry_in_scan_owed") as tx:
        for rel in sorted(saved):
            record_scan_owed(tx, OwedScan(commit, rel), now, reason)


def record_committed_save(workspace: Path, paths: list[Path], commit: str) -> str | None:
    """Append pending facts for one written guidance commit; return a receipt line only on failure.

    Runs after the commit landed, so a failure here never undoes the save. The
    request does one ``git grep``, looks up at most ``SAVE_OWNER_BUDGET`` owners
    and resolves at most ``SAVE_CASE_BUDGET`` candidate lessons; a larger scan,
    or a failed one, becomes owed scans the drain runs.
    """
    from contextlib import closing

    from infra.carry_in_drain import request_drain
    from infra.database import get_db

    saved: set[str] = set()
    try:
        root = workspace.resolve()
        saved = {
            path.resolve(strict=False).relative_to(root).as_posix()
            for path in paths if path.resolve(strict=False).is_relative_to(root)
        }
        keys = lesson_keys(workspace) if saved else {}
        if not keys:
            return None
        trays: dict[str, Path | None] = {}
        with closing(get_db()) as db:
            lessons = candidate_lessons(db, workspace, saved, keys, owner_budget=SAVE_OWNER_BUDGET, trays=trays)
            within = lessons is not None and len(lessons) <= SAVE_CASE_BUDGET
            inserted = record_cases(db, workspace, saved, commit, lessons, trays=trays) if within and lessons else 0
        if not within:
            _owe_scans(saved, commit, "over_budget")
            request_drain(workspace)
            return None
    except Exception:  # the save already committed; hand the scan to the drain, never undo the save
        logger.opt(exception=True).warning("carry-in pending facts not recorded for {}", commit)
        try:
            if not saved:
                raise RuntimeError("no saved path to retry")
            _owe_scans(saved, commit, "failed")
        except Exception:
            logger.opt(exception=True).error("carry-in scan for {} could not be scheduled", commit)
            return SCAN_LOST.format(ref=commit[:12])
        request_drain(workspace)
        return SCAN_RETRYING
    if inserted:
        request_drain(workspace)
    return None
