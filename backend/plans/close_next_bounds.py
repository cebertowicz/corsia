"""Keeping the ``next`` block inside the bytes a tool result can spend on it.

The block a close returns, and the one a wake carries, is read by a model, so
its size is part of its contract. This module owns that: the byte caps, the
measure that matches what the tool actually emits, and the fit that spends the
remaining room on the selected task's acceptance criteria before it drops any
listed row.

It composes nothing and reads no state, so ``plans.close_next`` depends on it
and it depends only on the plan's title limit.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from plans.service import MAX_NODE_TITLE
from prompts import plan_tools as text


def json_bytes(value: Any) -> int:
    """Return the size the tool emits for a value: sorted-key, ASCII-escaped JSON."""
    return len(json.dumps(value, sort_keys=True))


#: Serialized-byte caps, measured as the tool emits them (``json_bytes``). The
#: selected task's criteria take the bytes the rest of the block leaves, at least
#: ``CRITERIA_MIN_BYTES``: with every other field at its cap, 36-character ids
#: and 10-digit revisions, that floor still fits beside three rows and a named
#: refusal. A slot-held reason cuts the holder's title, never its action.
NEXT_BLOCK_MAX_BYTES = 2048
#: Every emitted node title, in every plan read, wake and reply. Derived from the
#: title input limit (``service.MAX_NODE_TITLE``, the one-line pane row): the
#: emitted size of an ASCII title at that limit, so a title the plan accepts is
#: never cut. A title in another script, or one older than the limit, ends in
#: the ellipsis at the same byte cost, so a list of titles costs the same bytes
#: whatever script it is in.
TITLE_MAX_BYTES = json_bytes("x" * MAX_NODE_TITLE)
WAIT_MAX_BYTES = 72
FEEDBACK_MAX_BYTES = 480
#: A rejection's handoff is its holder's whole brief, sent once per rejection,
#: so its feedback and criteria are bounded only by this safety ceiling (#2835).
REVISION_BRIEF_MAX_BYTES = 16384
CRITERIA_MIN_BYTES = 640


class CloseNextInvariantError(RuntimeError):
    """The close found state its owners guarantee cannot exist.

    Not a plan conflict: re-reading the plan and retrying cannot repair an
    internal invariant break, so this is raised as an internal error rather
    than the retryable conflict class, and the whole mutation rolls back.
    """


def bounded(value: str, max_bytes: int) -> str:
    """Bound one label by the bytes the tool emits for it."""
    return text.bounded_text(value, max_bytes, measure=json_bytes)


def title(value: str) -> str:
    """Return a node title as every plan surface emits it (``TITLE_MAX_BYTES``)."""
    return bounded(value, TITLE_MAX_BYTES)


def slot_held_reason(state: str, holder) -> str:
    """Return the slot-held action naming the holder by its title, cut like any title."""
    return text.slot_held_reason(title(str(holder["title"])), state)


def fit(
    block: dict[str, Any], total: int, criteria: str | None,
    label: Callable[[int, int], str] = text.listed_text,
) -> dict[str, Any]:
    """Fit the block to its byte cap, giving the selected task's criteria the rest.

    Listed rows drop from the tail only while the block, with its criteria at
    their floor, is still over the cap; each drop gives the criteria more room.

    Raises:
        CloseNextInvariantError: The block is over its cap with no rows left,
            which only ids far longer than any generated id can cause.
    """
    rows = block["open_tasks"]
    while True:
        if criteria is not None:
            block["selected"]["acceptance_criteria"] = _criteria_in_room(block, criteria)
        if json_bytes(block) <= NEXT_BLOCK_MAX_BYTES:
            return block
        if not rows:
            raise CloseNextInvariantError(
                "next block exceeds its byte cap with no rows listed"
            )
        rows.pop()
        block["open_tasks_listed"] = label(len(rows), total)


def _criteria_in_room(block: dict[str, Any], criteria: str) -> str:
    """Return the criteria cut to the bytes the rest of the block leaves them."""
    block["selected"]["acceptance_criteria"] = ""
    room = NEXT_BLOCK_MAX_BYTES - json_bytes(block) + json_bytes("")
    return text.bounded_text(
        criteria, max(room, CRITERIA_MIN_BYTES),
        measure=json_bytes, marker=text.CRITERIA_CUT,
    )
