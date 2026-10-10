"""Making room when a lesson would push its destination past the cap.

WORKSPACE-STANDARD §Caps says over the cap means something belongs elsewhere.
When a staged placement would pass the cap, the checkpoint fork is shown the
destination as numbered lines and answers ordered ``room`` operations in the
same tool loop; the backend applies them only when every one is checkable:

* ``fold``: the lesson merges into list item N, which gets the new text (at
  most one fold, and the caller confirms the lesson is in it);
* ``remove``: line N goes, because a quoted line of the same file supersedes
  it (``superseded-by``) or a quoted passage of an existing workspace file
  already handles it (``handled-by``);
* ``move``: lines N..M go verbatim to the overflow file the cap table names,
  leaving a one-line pointer the backend writes.

Headings, required sections (Mission, Authority, Does not own, with their
subsections) and skill frontmatter are never touched. Anything unchecked, or a
result still over the cap, means no room: nothing is written. The cap measure
is ``placement_decide.over_cap``, the same one that sent the write here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.placement_decide import (
    CONTEXT_LESSONS,
    CONTEXT_WORDS,
    InvalidDecision,
    lesson_count,
    over_cap,
    says,
    skill_body,
    skill_body_cap,
    words,
)

_MAX_OPERATIONS = 12
_HEADING = re.compile(r"^(#+)\s")
_PROTECTED = re.compile(r"^#+\s*(mission|authority|does[- ]not[- ]own)\b", re.IGNORECASE)
_ITEM = re.compile(r"^\s*[-*]\s+\S")

GUIDE = """room: the fewest ordered operations that make room in the file \
below. Line numbers refer to the numbered file as given.

- fold: the lesson belongs inside an existing list item; give its line and
  the merged text: one line that contains the lesson word for word and keeps
  that item's own meaning. At most one fold.
- remove: a line the file no longer needs. Only two bases count:
  superseded-by: another line of this same file now says it; quote that line.
  handled-by: an existing workspace file already handles it; give its path
  relative to the workspace and quote the passage that covers it.
- move: lines start..end hold detail (history, procedure) that does not change
  a first decision; they go verbatim to the overflow file and leave a pointer.
  Only offered when an overflow file is named.

Never touch a heading, a Mission, Authority or Does-not-own section, or
frontmatter.
If nothing honest makes room, pass room as an empty list."""

#: One make-room operation, as the ``room`` list of ``continuity_edit`` takes it.
OPERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op", "start", "end", "text", "basis", "path", "quote"],
    "properties": {
        "op": {"type": "string", "enum": ["fold", "remove", "move"]},
        "start": {"type": "integer"},
        "end": {"type": ["integer", "null"]},
        "text": {"type": ["string", "null"]},
        "basis": {
            "type": ["string", "null"],
            "enum": ["superseded-by", "handled-by", None],
        },
        "path": {"type": ["string", "null"]},
        "quote": {"type": ["string", "null"]},
    },
}


@dataclass(frozen=True)
class Operation:
    op: str
    start: int
    end: int
    text: str | None = None
    basis: str | None = None
    path: str | None = None
    quote: str | None = None

    def describe(self) -> str:
        """One commit-body line: what changed and on what basis."""
        span = f"line {self.start}" if self.start == self.end else f"lines {self.start}-{self.end}"
        if self.op == "remove":
            ref = self.path if self.basis == "handled-by" else "a line in this file"
            return f"remove {span} ({self.basis} {ref})"
        return f"{self.op} {span}"


@dataclass(frozen=True)
class Room:
    """Operations proposed against the exact destination text ``base``."""

    base: str
    operations: tuple[Operation, ...]


def validate_operations(raw: object) -> tuple[Operation, ...]:
    """Refuse a make-room answer outside the contract."""
    if not isinstance(raw, dict) or not isinstance(raw.get("operations"), list):
        raise InvalidDecision("make-room answer must carry an operations list")
    items = raw["operations"]
    if len(items) > _MAX_OPERATIONS:
        raise InvalidDecision("too many make-room operations")
    if sum(1 for item in items if isinstance(item, dict) and item.get("op") == "fold") > 1:
        raise InvalidDecision("a lesson folds into one list item")
    parsed = []
    for item in items:
        if not isinstance(item, dict) or item.get("op") not in ("fold", "remove", "move"):
            raise InvalidDecision("unknown make-room operation")
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, int) or (end is not None and not isinstance(end, int)):
            raise InvalidDecision("operation lines must be integers")
        end = start if end is None or item["op"] != "move" else end
        text = item.get("text")
        parsed.append(Operation(
            op=item["op"], start=start, end=end,
            text=" ".join(text.split()) if isinstance(text, str) else None,
            basis=item.get("basis") if isinstance(item.get("basis"), str) else None,
            path=item.get("path") if isinstance(item.get("path"), str) else None,
            quote=item.get("quote") if isinstance(item.get("quote"), str) else None,
        ))
    return tuple(parsed)


def build_prompt(text: str, lesson: str, need: str, overflow: str | None) -> str:
    numbered = "\n".join(f"{n}: {line}" for n, line in enumerate(text.split("\n"), 1))
    return (
        f"Lesson to add:\n{lesson}\n\nRoom needed: {need}\n"
        f"Overflow file: {overflow or 'none (move is not available)'}\n\n"
        f"File:\n{numbered}"
    )


def need(kind: str, text: str) -> str:
    """How far over the cap ``text`` (with the lesson added) is."""
    if kind == "skill":
        return f"body is {words(skill_body(text))} words; cap {skill_body_cap(text)}"
    return (
        f"{words(text)} words (cap {CONTEXT_WORDS}); "
        f"{lesson_count(text)} lessons (cap {CONTEXT_LESSONS})"
    )


def overflow_file(kind: str, destination: Path, reference_dir: Path | None) -> Path | None:
    """The cap table's overflow: domain reference for history, a skill's reference."""
    if kind == "skill":
        return destination.parent / "reference.md"
    if reference_dir is not None:
        return reference_dir / "LESSONS-HISTORY.md"
    return None


def _protected_lines(lines: list[str]) -> set[int]:
    """1-based headings, leading frontmatter, and required sections.

    A required section runs to the next heading of its own level or higher,
    so its subsections stay protected with it.
    """
    protected: set[int] = set()
    if lines and lines[0] == "---":
        close = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
        protected.update(range(1, (close or 0) + 2))
    guard_level = 0
    for number, line in enumerate(lines, 1):
        heading = _HEADING.match(line)
        if heading:
            protected.add(number)
            level = len(heading.group(1))
            if guard_level and level <= guard_level:
                guard_level = 0
            if not guard_level and _PROTECTED.match(line):
                guard_level = level
        if guard_level:
            protected.add(number)
    return protected


def _basis_holds(
    operation: Operation,
    lines: list[str],
    changed: set[int],
    workspace: Path,
    destination: Path,
) -> bool:
    quote = operation.quote or ""
    if operation.basis == "superseded-by":
        # One unchanged line must say it: a quote stitched across lines, or
        # across a line this room edits, proves nothing.
        return any(
            says(line, quote)
            for number, line in enumerate(lines, 1) if number not in changed
        )
    if operation.basis == "handled-by" and operation.path:
        root = workspace.resolve()
        target = (root / operation.path).resolve()
        if (
            not target.is_relative_to(root) or not target.is_file()
            or target == destination.resolve()
        ):
            return False
        return says(target.read_text(), quote)
    return False


def _checked_spans(
    room: Room, current: str, workspace: Path, destination: Path, overflow: Path | None
) -> list[Operation] | None:
    """The operations, if each targets unchanged, unprotected lines with a basis.

    A ``superseded-by`` quote must sit in a line no operation changes, so the
    superseding text is still in the file afterwards.
    """
    if current != room.base:
        return None
    lines = current.split("\n")
    protected = _protected_lines(lines)
    taken: set[int] = set()
    changed = {n for op in room.operations for n in range(op.start, op.end + 1)}
    for operation in room.operations:
        span = set(range(operation.start, operation.end + 1))
        if (
            operation.start < 1 or operation.end < operation.start
            or operation.end > len(lines) or span & taken or span & protected
            or not any(lines[n - 1].strip() for n in span)
        ):
            return None
        if operation.op == "fold" and (
            not operation.text or not _ITEM.match(lines[operation.start - 1])
        ):
            return None
        if operation.op == "remove" and not _basis_holds(
            operation, lines, changed, workspace, destination
        ):
            return None
        if operation.op == "move" and overflow is None:
            return None
        taken |= span
    return list(room.operations)


def apply(
    room: Room,
    *,
    kind: str,
    current: str,
    add_lesson,
    workspace: Path,
    destination: Path,
    overflow: Path | None,
    overflow_current: str | None,
    day: str,
) -> tuple[str, str | None, list[Operation]] | None:
    """Destination text, overflow text and applied operations; None when no room.

    ``add_lesson(text)`` appends the lesson unless a ``fold`` takes its place;
    a fold is trusted to carry it only after the caller finds the lesson in
    the result (``placement_outcomes._made_room``, ``fold-lost-lesson``).
    ``overflow_current`` is the overflow file as the caller read it (None when
    absent), the same read its expected digest comes from. Moved lines are
    copied verbatim by construction and the pointer names the overflow file,
    so neither needs a check after the fact.
    """
    operations = _checked_spans(room, current, workspace, destination, overflow)
    if operations is None or not operations:
        return None
    lines = current.split("\n")
    replaced: dict[int, list[str]] = {}
    moved: list[str] = []
    pointer_to = _relative(overflow, workspace) if overflow is not None else ""
    for operation in sorted(operations, key=lambda op: op.start):
        span = range(operation.start, operation.end + 1)
        if operation.op == "fold":
            replaced[operation.start] = [_as_item(lines[operation.start - 1], str(operation.text))]
        elif operation.op == "remove":
            replaced.update({n: [] for n in span})
        else:
            moved.extend(lines[n - 1] for n in span)
            replaced.update({n: [] for n in span})
            replaced[operation.start] = [f"Moved to {pointer_to} on {day}."]
    rebuilt = [
        row for number, line in enumerate(lines, 1)
        for row in replaced.get(number, [line])
    ]
    text = "\n".join(rebuilt)
    if not any(op.op == "fold" for op in operations):
        text = add_lesson(text)
    overflow_text = None
    if moved and overflow is not None:
        prior = overflow_current if overflow_current is not None else f"# {overflow.stem}\n"
        overflow_text = (
            prior.rstrip("\n")
            + f"\n\n## Moved from {_relative(destination, workspace)} on {day}\n\n"
            + "\n".join(moved) + "\n"
        )
    if over_cap(kind, text):
        return None
    return text, overflow_text, operations


def _as_item(original: str, text: str) -> str:
    prefix = re.match(r"^(\s*(?:[-*]\s+)?(?:\d{4}-\d{2}-\d{2}:\s+)?)", original)
    return f"{prefix.group(1) if prefix else ''}{text}"


def _relative(path: Path, workspace: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)
