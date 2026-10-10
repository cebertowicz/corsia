"""The one authorized truncation: fill a too-small window, and say what fell out.

When a destination's context window cannot hold this thread — not even a
minimum checkpoint plus the exact tail — there used to be exactly one answer:
stop. That is the ``destination_fit_blocked`` wedge, and stopping is the one
thing this system may not do.

The authorized degradation instead (product owner, 2026-08-08): perform ONE
cold fill sized to the target window, and DECLARE the truncation to the model.
The reasoning was recorded rather than assumed — doing it "right", folding the
full history over multiple provider turns, was judged unnecessary, slow and
expensive for a case this rare, and the model can recover what it needs from
past checkpoints, plans and searchable history.

This module owns only the pure part: which progressively smaller replays exist,
and what to tell the model about the one that was used. Measuring which of them
fits is the renderer's job, because only the renderer owns the fit ledger.

Truncation is NEVER silent. A cold fill that the model is not told about is a
defect no matter how gracefully anything renders it.
"""

from __future__ import annotations

from dataclasses import replace

from core.context_replay import ReplaySelection
from history.types import (
    PortableHistoryError,
    PortableHistoryProjection,
)
from history.projection import slice_portable_projection


def cold_fill_candidates(
    selection: ReplaySelection,
) -> tuple[ReplaySelection, ...]:
    """Progressively smaller replays of one thread, largest first.

    Each candidate drops whole TURNS from the oldest end, because a turn is the
    smallest unit that is coherent on its own: cutting inside one hands the
    model an assistant step whose tool results are gone, which is worse than
    not sending it. The newest turn is never dropped — a replay with no recent
    history is not a degraded answer, it is a different conversation.

    The first element is the untruncated selection, so a caller can measure the
    whole ladder in one loop and discover it never needed to truncate at all.
    """
    turns = selection.tail.turns
    candidates: list[ReplaySelection] = [selection]
    for turn in turns[:-1]:
        boundary = turn.boundary
        if boundary is None:
            continue
        try:
            sliced = slice_portable_projection(
                selection.tail,
                start_exclusive=boundary,
            )
        except PortableHistoryError:
            # Not a legal cut in this frozen projection. Skipping it costs one
            # rung of granularity; forcing it would emit a projection whose
            # boundaries lie about what it contains.
            continue
        candidates.append(replace(selection, tail=sliced))
    return tuple(candidates)


def dropped_turn_count(
    original: PortableHistoryProjection,
    chosen: PortableHistoryProjection,
) -> int:
    """How many whole turns the chosen cold fill left behind."""
    return max(0, len(original.turns) - len(chosen.turns))


