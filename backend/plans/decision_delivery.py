"""Project one canonical decision answer into its owner's existing FIFO."""

from __future__ import annotations

import json

from infra.input_admission import AcceptedInput
from plans.decision_fields import (
    OTHER_OPTION_ID, PICK_ANY, answer_label, answer_note, chosen_ids, pick_of, recommended_ids,
)


def _listed(options: list[dict], ids: list[str]) -> str:
    """Each option as a JSON string `"A — label"`, in option order, joined."""
    return ", ".join(
        json.dumps(f"{option['id']} — {option['label']}", ensure_ascii=False)
        for option in options if option["id"] in ids
    )


def _format_pick_any(decision: dict, answer: dict) -> str:
    """Spell out chosen, not chosen, the note and how the recommendation fared."""
    chosen = chosen_ids(decision, answer)
    options = decision["options"]
    rest = [option["id"] for option in options if option["id"] not in chosen]
    recommended = recommended_ids(decision)
    verdict = "matched" if set(chosen) == set(recommended) else "changed by the person"
    lines = [
        "[PLAN DECISION ANSWERED]",
        f"Decision: {json.dumps(str(decision['title']), ensure_ascii=False)}",
        f"Decision ID: {decision['id']}",
        f"Chosen: {_listed(options, chosen) if chosen else 'none'}",
        f"Not chosen: {_listed(options, rest) if rest else 'none'}",
    ]
    note = answer_note(answer)
    if note:
        lines.append(f"Note: {json.dumps(note, ensure_ascii=False)}")
    lines.append(
        f"Your recommendation: {', '.join(recommended) or 'none'} ({verdict})"
    )
    return "\n".join(lines)


def format_answer_input(decision: dict, answer: dict) -> str:
    """Freeze one self-contained answer for the owning agent's first screen."""
    if pick_of(decision) == PICK_ANY:
        return _format_pick_any(decision, answer)
    option_id = answer_label(decision, answer)
    if option_id == OTHER_OPTION_ID:
        selected = f"Other — {answer['text']}"
    else:
        option = next(
            option for option in decision["options"] if option["id"] == option_id
        )
        selected = f"{option_id} — {option['label']}"
    return (
        "[PLAN DECISION ANSWERED]\n"
        f"Decision: {json.dumps(str(decision['title']), ensure_ascii=False)}\n"
        f"Decision ID: {decision['id']}\n"
        f"Answer: {json.dumps(selected, ensure_ascii=False)}"
    )


def append_answer_input(
    db, decision: dict, answer: dict,
) -> AcceptedInput | None:
    """Append the answer as input to the plan owner, unless that thread cannot take input.

    The answer route already refuses an archived or purged plan owner inside
    this write, so the unavailable case is only a guard for an inconsistent
    thread row.

    Written through ``plan_wake.append_input``, the one writer for this
    domain's result-bearing inputs. A model-less owner is deliberately *not*
    skipped here: unlike news about someone else's work, the answer is the
    result this thread asked for, so it is kept at the FIFO head until the
    user chooses a model (``input_admission``) rather than dropped.
    """
    from plans import plan_wake

    return plan_wake.append_input(
        db,
        recipient=str(decision["thread_id"]),
        message_id=f"plan-decision-answer:{decision['id']}",
        content=format_answer_input(decision, answer),
        metadata={
            "event_type": "plan.decision_answered",
            "origin": "plan",
            "plan_id": decision["plan_id"],
            "decision_id": decision["id"],
        },
    )
