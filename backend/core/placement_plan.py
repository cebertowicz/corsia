"""The one commit plan for writing a lesson into its destination file.

``core.placement_decide`` chooses the destination; this builds what the commit contains: the new text,
the make-room operations, and the digest each written file is compared against. ``core.placement_outcomes``
commits it and ends the entry, so the write and the answer-time loadability check validate the same bytes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from core.placement_decide import (
    SKILL_DESCRIPTION_WORDS,
    Decision,
    Destination,
    ThreadPlace,
    append_lesson,
    append_to_reference,
    append_to_skill,
    over_cap,
    says,
    words,
)
from core import placement_room
from core.placement_room import Operation, Room
from infra.guidance_history_paths import bytes_before_text, digest

ALREADY = "already-at-destination"
_Plan = tuple[dict[Path, str], dict[Path, str | None], list[Operation]]


def today() -> str:
    """The lesson date, in the local time of the host the user runs CORSIA on."""
    return date.today().isoformat()


def relative_path(path: Path) -> str:
    from infra.config import WORKSPACE

    try:
        return str(path.resolve().relative_to(Path(WORKSPACE).resolve()))
    except ValueError:
        return str(path)


def room_request(
    place: ThreadPlace, decision: Decision, destination: Destination
) -> tuple[str, str] | None:
    """The base text and numbered-file request when this write would pass its cap."""
    path = destination.path
    if path is None or destination.kind not in ("context", "skill") or not path.exists():
        return None
    current = path.read_text()
    if decision.duplicate_of and says(current, decision.duplicate_of):
        return None
    if _over_cap(destination.kind, current, decision) is None:
        return None
    overflow = placement_room.overflow_file(destination.kind, path, place.reference_dir)
    after = _new_text(destination.kind, current, decision)
    return current, placement_room.build_prompt(
        current, decision.text, placement_room.need(destination.kind, after),
        relative_path(overflow) if overflow is not None else None,
    )


def room_refusal(
    place: ThreadPlace, decision: Decision, destination: Destination, room: Room
) -> str | None:
    """Why ``room`` cannot make room in its base text, or None when it can.

    The same check the write runs, applied to the text the fork was shown,
    so a room that can never apply goes back to the fork instead of settling
    ``no-room`` after it is gone.
    """
    # This answer is only inspected for why a room cannot apply, so it plans no expectation and reads nothing for one.
    made = _made_room(place, decision, destination, room.base, room, path_bytes=None)
    return made if isinstance(made, str) else None


def commit_plan(
    place: ThreadPlace, decision: Decision, destination: Destination, room: Room | None,
) -> _Plan | str:
    """The one commit plan for this answer, or why it writes nothing.

    ``write`` commits it and the answer-time loadability check validates it,
    so both see the same bytes. ``ALREADY`` when the file already says the
    lesson; otherwise the ``unplaced`` reason.
    """
    path = destination.path
    assert path is not None
    raw = bytes_before_text(path)  # the expectation of the text read below, taken before it
    current = path.read_text() if path.exists() else None
    if decision.duplicate_of and current is not None and says(current, decision.duplicate_of):
        return ALREADY
    over = _over_cap(destination.kind, current, decision)
    if over is None:
        # The expectation is the bytes that read came from: text rewrites line endings it keeps no record of.
        return {path: _new_text(destination.kind, current, decision)}, {path: digest(raw)}, []
    if room is None:
        return over
    if current != room.base:
        # The fork made room against text that is gone; only a fresh
        # review can make room again, so the next checkpoint reconsiders.
        return "destination-changed"
    return _made_room(place, decision, destination, current, room, path_bytes=raw)


def _made_room(
    place: ThreadPlace,
    decision: Decision,
    destination: Destination,
    current: str | None,
    room: Room,
    *,
    path_bytes: bytes | None,
) -> tuple[dict[Path, str], dict[Path, str | None], list[Operation]] | str:
    """The commit's files and expected digests after making room, or why not.

    ``path_bytes`` is the destination's bytes as they were read before ``current`` was, so each
    expectation is bound to a read rather than rebuilt after one. ``no-room`` when the operations do not
    check out; ``fold-lost-lesson`` when a fold replaced the appended lesson but the file does not say it.
    """
    from infra.config import WORKSPACE

    path = destination.path
    if current is None or path is None:
        return "no-room"
    overflow = placement_room.overflow_file(destination.kind, path, place.reference_dir)
    overflow_raw = bytes_before_text(overflow) if overflow is not None else None
    overflow_current = (
        overflow.read_text() if overflow is not None and overflow.exists() else None
    )
    applied = placement_room.apply(
        room, kind=destination.kind, current=current,
        add_lesson=lambda text: _new_text(destination.kind, text, decision),
        workspace=Path(WORKSPACE), destination=path, overflow=overflow,
        overflow_current=overflow_current, day=today(),
    )
    if applied is None:
        return "no-room"
    text, overflow_text, operations = applied
    if any(op.op == "fold" for op in operations) and not says(text, decision.text):
        return "fold-lost-lesson"
    # Each expectation is the bytes its text was read from, never a hash taken after that read.
    writes, expected = {path: text}, {path: digest(path_bytes)}
    if overflow is not None and overflow_text is not None:
        writes[overflow] = overflow_text
        expected[overflow] = digest(overflow_raw)
    return writes, expected, operations


def _over_cap(kind: str, current: str | None, decision: Decision) -> str | None:
    """The reason a write would pass its WORKSPACE-STANDARD cap, or None."""
    if kind not in ("context", "skill"):
        return None
    if over_cap(kind, _new_text(kind, current, decision)):
        return "over-cap-unplaced"
    description = decision.skill_description or decision.text
    if kind == "skill" and current is None and words(description) > SKILL_DESCRIPTION_WORDS:
        return "over-cap-unplaced"
    return None


def _new_text(kind: str, current: str | None, decision: Decision) -> str:
    if kind == "context":
        return append_lesson(current, decision.text, today())[0]
    if kind == "skill":
        return append_to_skill(
            current, str(decision.skill_name), decision.text,
            " ".join((decision.skill_description or "").split()) or None,
        )
    return append_to_reference(current or "", decision.text)
