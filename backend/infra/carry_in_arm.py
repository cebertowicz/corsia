"""One carry-in sample side: its prompt and the lesson delivery inside it.

Split from ``infra.carry_in_drain``, which runs the samples. A re-test changes
one thing between its sides. A trial lesson's sides see the same target
revision and differ only in the delivery, which follows task start's omission
rule; a target edit of any other lesson compares the revision before and after
with no delivery. A Turn's recorded input must equal its side's prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.carry_in_facts import CarryInFact, EligibleCase
from eval.tool_allowlist import CarryInUnseen

if TYPE_CHECKING:
    from core.lesson_delivery import TrialDelivery


def delivery_block(delivery: TrialDelivery) -> str:
    """The ``trial_lessons`` result task start returns to an agent, as text for a sample's input."""
    from core.lesson_delivery import trial_lesson

    return json.dumps({"trial_lessons": [trial_lesson(delivery)]}, ensure_ascii=False, indent=2)


def unseen_by(delivery: TrialDelivery, *, deliver: bool) -> CarryInUnseen:
    """What this side's read tools may not observe of the lesson: never its case, and the baseline
    not the lesson at all.

    Both sides lose the lesson's skill directory and its owner's captured hypotheses
    (``lesson_files``), which keep the case of a trial they became. A delivered side still reads the
    lesson file, as it was when delivered, without its case.
    """
    from core import lesson_files

    source = delivery.source_path
    hidden = (source.parent, lesson_files.capture_path(source.parents[2], "-").parent)
    if not deliver:
        return CarryInUnseen(hidden)
    view = lesson_files.without_case(delivery.source_data.decode("utf-8", errors="replace"))
    return CarryInUnseen(hidden, source, view.encode("utf-8"))


@dataclass(frozen=True)
class Arm:
    """One side's prompt and what its read tools may not observe."""

    prompt: str
    unseen: CarryInUnseen = CarryInUnseen()

    def carried_by(self, recorded: str | None) -> bool:
        """Whether a Turn's recorded input is exactly this side's prompt."""
        return recorded == self.prompt


def turn_input(db, thread_id: str, turn_id: str) -> str | None:
    """The user input rows the dispatch recorded for one Turn, in order; None when it has none."""
    rows = db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND active_turn_id=? AND role='user' "
        "ORDER BY input_order, seq",
        (thread_id, turn_id),
    ).fetchall()
    return "\n".join(str(row[0]) for row in rows) if rows else None


def sample_prompt(
    case: dict[str, Any], target_path: str, blob: bytes | None,
    block: str | None = None, *, show_file: bool = True,
) -> str:
    """The situation, the exact target revision and the lesson delivery if any; never the right call.

    ``show_file`` is False when the target is the lesson's own file: a baseline must never see it.
    """
    lines = [str(case["what_happened"]).strip()]
    if show_file:
        lines.append("")
        if blob is None:
            lines.append(f"There is no file at `{target_path}` yet.")
        else:
            text = blob.decode("utf-8", errors="replace")
            fence = "`" * max(3, _longest_backtick_run(text) + 1)
            lines += [f"`{target_path}` reads:", fence, text.rstrip("\n"), fence]
    if block:
        lines += ["", block]
    return "\n".join(lines)


def arm(fact: CarryInFact, case: EligibleCase, blob: bytes | None, *, deliver: bool) -> Arm:
    block = delivery_block(case.delivery) if deliver else None
    prompt = sample_prompt(case.case, fact.target_path, blob, block, show_file=fact.target_path != fact.lesson_path)
    return Arm(prompt, unseen_by(case.delivery, deliver=deliver))


def arms(fact: CarryInFact, case: EligibleCase, before: bytes | None, after: bytes) -> tuple[Arm, Arm]:
    """The baseline and candidate sides of one re-test; they differ in exactly one thing.

    A trial lesson's candidate carries the delivery unless task start would omit it
    for this target text (``core.lesson_delivery.target_omission``; the equal prompts
    then retire the case); a lesson in any other state compares ``before`` with
    ``after`` and delivers nothing.
    """
    from core.lesson_delivery import target_omission

    if not case.deliver:
        return arm(fact, case, before, deliver=False), arm(fact, case, after, deliver=False)
    omitted = target_omission(case.delivery, after.decode("utf-8", errors="replace")) is not None
    return arm(fact, case, after, deliver=False), arm(fact, case, after, deliver=not omitted)


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest
