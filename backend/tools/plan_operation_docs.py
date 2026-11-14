"""Per-operation shape guidance, generated from the field contract itself.

The operation enum alone tells a caller which verbs exist and nothing about
what each one needs, so the shape had to be learned from rejections: which keys
an operation requires, and that every one of them -- ``expected_revision``
included -- belongs inside ``params``. Both facts already live in
``plans.boundary_fields``; this renders them, so the published guidance cannot
drift from the contract that enforces it.
"""

from __future__ import annotations

from typing import Iterable

#: The note shown beside an operation's required keys: the state it needs,
#: which is otherwise only learned by a refusal, and when another
#: operation fits better. Keyed by operation.
_OPERATION_NOTES = {
    "pause": "conductor or CORSIA agent on another agent's plan, from plan_read(thread_id=...)",
    "resume": "conductor or CORSIA agent on another agent's plan, from plan_read(thread_id=...)",
    # One first choice for each neighboring settlement state. Active accept
    # remains runtime-compatible, but the published vocabulary never recommends it.
    "mark_done": (
        "active self-completion, or a user step once really done (evidence in "
        "plain words, they read it; never an approval); accepts as "
        "Done without another reviewer"
    ),
    "request_review": "active handoff; leaves work awaiting another reviewer",
    "accept": "review_requested; verified complete",
    "accept_with_follow_up": (
        "review_requested; verified complete and a new follow-up is required"
    ),
    # A step replaced by a re-brief or handed over is not a failure (#2232):
    # the three verbs an agent weighs name which one fits.
    "reject": "review_requested; sent back, not failed; or accept_with_follow_up",
    "fail": "unmet; replaced work: close_node",
    "add_correction": "settled",
    "close_node": "open; unneeded; not done, not failed",
}

#: Operations with more than one valid body, which one required set cannot
#: express. The text replaces the generated key list for that operation.
_ALTERNATE_SHAPES = {
    "add": "title, acceptance_criteria; task size, intent",
    "insert": "title, acceptance_criteria; task size, intent",
    "archive_node": "one: node_id, expected_revision; many: node_ids",
    "edit": "one: node_id, expected_revision + the change; many: node_ids,"
            " later/parent_id",
    "close_node": "one: node_id, expected_revision, reason; many: node_ids, reason",
}

#: Keys most operations need. Stated once, so each line lists only its extras.
_COMMON_KEYS = frozenset({"node_id", "expected_revision"})

PARAMS_DESCRIPTION = "Operation arguments go here."


def _shape(operation: str, required: set[str]) -> str:
    """Return one operation's required keys, relative to the common pair."""
    alternate = _ALTERNATE_SHAPES.get(operation)
    note = _OPERATION_NOTES.get(operation)
    if alternate:
        # A verb with two bodies still carries its note: which choice it is stays
        # beside its shape, where the agent is already reading it.
        return alternate + (f" ({note})" if note else "")
    if _COMMON_KEYS <= required:
        extras = sorted(required - _COMMON_KEYS)
        # "+" is defined by the header line, so a verb that adds nothing says so
        # instead of repeating the pair it shares with most of the vocabulary.
        text = "+ " + ", ".join(extras) if extras else "+ none"
    else:
        text = ", ".join(sorted(required))
    note = _OPERATION_NOTES.get(operation)
    return text + (f" ({note})" if note else "")


def operation_description(
    operations: Iterable[str],
    required_fields: dict[str, set[str]],
    *,
    preamble: str,
) -> str:
    """Return the operation enum's description: required params keys, grouped.

    Operations with an identical shape share one line, so the text grows with
    the number of distinct shapes rather than the number of operations.
    """
    grouped: dict[str, list[str]] = {}
    for operation in operations:
        grouped.setdefault(_shape(operation, required_fields[operation]), []).append(operation)
    relative = any(shape.startswith("+ ") for shape in grouped)
    lines = [preamble] if preamble else []
    lines.append(
        "Required params"
        + (" ('+' adds to node_id, expected_revision)" if relative else "")
        + ":"
    )
    lines.extend(f"- {', '.join(ops)}: {shape}" for shape, ops in grouped.items())
    return "\n".join(lines)
