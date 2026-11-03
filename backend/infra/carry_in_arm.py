"""One carry-in sample side: its prompt, the lesson delivery inside it, and its receipt.

Split from ``infra.carry_in_drain``, which runs the samples. A candidate prompt
carries the delivery the way task start returns it (``core.lesson_delivery``); a
baseline prompt carries none, and its Turn's recorded input must say so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.carry_in_facts import CarryInFact, EligibleCase

if TYPE_CHECKING:
    from core.lesson_delivery import TrialDelivery


#: How a delivery block opens; a baseline input never contains it.
_BLOCK_OPEN = '{\n  "trial_lessons": ['


def delivery_block(delivery: TrialDelivery) -> str:
    """The ``trial_lessons`` result task start returns to an agent, as text for a sample's input."""
    from core.lesson_delivery import trial_lesson

    return json.dumps({"trial_lessons": [trial_lesson(delivery)]}, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class Arm:
    """One side's prompt; ``block`` is the lesson delivery inside it, None for a side with none."""

    prompt: str
    delivery: TrialDelivery
    block: str | None

    def carried_by(self, recorded: str | None) -> bool:
        """Whether a Turn's recorded input holds exactly this side's delivery."""
        if recorded is None:
            return False
        return self.block in recorded if self.block else _BLOCK_OPEN not in recorded


def turn_input(db, thread_id: str, turn_id: str) -> str | None:
    """The user input rows the dispatch recorded for one Turn, in order; None when it has none."""
    rows = db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND active_turn_id=? AND role='user' "
        "ORDER BY input_order, seq",
        (thread_id, turn_id),
    ).fetchall()
    return "\n".join(str(row[0]) for row in rows) if rows else None


def insert_receipt(db, thread_id: str, turn_id: str, arm: Arm, fact: CarryInFact) -> None:
    from core.guidance_loads import insert_guidance_load

    delivery = arm.delivery
    insert_guidance_load(
        db, thread_id=thread_id, turn_id=turn_id, path=delivery.source_path, data=delivery.source_data,
        returned_text=delivery.payload if arm.block else "", partial=False,
        variant="with" if arm.block else "without",
        lesson_id=fact.lesson_id, lesson_behavioral_sha256=fact.behavioral_sha256,
    )


def drop_receipts(db, thread_id: str) -> None:
    db.execute("DELETE FROM guidance_loads WHERE thread_id=?", (thread_id,))


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
    return Arm(prompt, case.delivery, block)


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest
