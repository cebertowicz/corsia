"""Model-facing text for a start refused because ready work ranks above it.

Text constants and text formatting functions only (package rule). The plans
domain decides when a start passes ready work (``plans.triage_lines``); this
module owns the words of the refusal, in its full form and in the short form a
``next`` block carries.
"""

from __future__ import annotations

#: Refusal of an owner's named start that passes ready top-level work.
START_OUT_OF_ORDER = (
    "Not started: '{title}' ({node}) is above this in plan order and is ready or held only "
    "by your own deadline check-back. Start it first, or reorder {mover}node_id={top} "
    "order_key={key}, then start this with {revision}. If it really waits on someone or "
    "something, set a wait that names it: block_node with an owner, a dependency, or a watch "
    "on the PR or event. Scheduling never puts off work you could move now. Reorder only if "
    "your priorities and the project's, read now, rank this higher. Otherwise start the "
    "item above, or ask the person."
)
#: A top-level task is the node the reorder moves, so its revision changes; a
#: step inside a phase is not moved, so its own revision still holds.
_REVISION_MOVED = "the expected_revision the reorder returns"
_REVISION_KEPT = "its own expected_revision, unchanged by the reorder"


#: The same refusal in a ``next`` block, no longer than ``_SLOT_HELD``'s fixed text.
#: Rank is judged at the top-level item, so a step inside a phase names its
#: phase: a reorder moves a node only among its siblings.
_START_OUT_OF_ORDER_COMPACT = "start {title} first, reorder {mover} above it, or name its wait"


def start_out_of_order_compact(title: str, *, root_kind: str | None = None) -> str:
    """Return the ``next`` block's short refusal naming the ready item by title.

    ``root_kind`` is the kind of the top-level item holding a task inside a
    phase or track: the reorder that clears the refusal moves that item, not
    the task. "its phase" is no longer than "this task", so the cap holds.
    """
    mover = "this task" if root_kind is None else f"its {root_kind}"
    return _START_OUT_OF_ORDER_COMPACT.format(title=title, mover=mover)


def start_out_of_order(
    title: str, node_id: str, top_id: str, key: int, *, root_kind: str | None = None,
) -> str:
    """Return the refusal for a start that passes the ready item ``node_id``.

    ``top_id`` is the top-level item to reorder; for a task inside a phase or
    track (``root_kind``) it is that item, named so, and the task keeps its
    revision.
    """
    revision = _REVISION_MOVED if root_kind is None else _REVISION_KEPT
    mover = "" if root_kind is None else f"its {root_kind} "
    return START_OUT_OF_ORDER.format(
        title=title, node=node_id, top=top_id, key=key, revision=revision, mover=mover,
    )
