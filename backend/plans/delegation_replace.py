"""Hand a delegated step to a replacement worker in one write (matrix row F6).

A parent that stands a worker down and hands the step to another used to
re-attach over it: the node pointed at the replacement, but the old worker's
steps stayed open under a plan nobody was reading, and a replacement that had
already started could not be attached at all. Replacing is one act with three
parts, each through its existing owner, so nothing here writes an attachment
or a node state itself:

1. the old worker's open steps close with reason ``replaced`` (the close
   lifecycle, via ``delegation_child_lifecycle.end_open_work``, which also
   releases anything the old worker had itself delegated);
2. the old attachment is detached (``delegation.detach_spawned_thread``);
3. the replacement is attached (``delegation.attach_spawned_thread``), even
   mid-work: steps it already ran keep their own commitment.

A step up for the parent's verdict on the old worker's return goes back to
``active``: that return is no longer what the parent judges, and the harvest
only takes up a step in work, so leaving it in review would drop the
replacement's return silently (#2310).

Every wake and wait about a delegated step reads the node's attachment, so from
this write on only the replacement can wake the parent about the step or hold
it waiting, and the pane shows the replacement as the step's worker.

Nothing here stops a thread. The old worker may still be in a Turn, and it may
itself have delegated: those workers keep running and their plans stay under
the old worker, which is still alive, so nothing moves them. Once detached,
none of them can reach the parent, so the result names them all and the caller,
who is the one acting, is told to stop them or adopt them onto a step of their
own (``delegation_adoption``); that is the reply, not a wake. The old worker
itself, if still live, gets one passive digest that it was replaced.
"""

from __future__ import annotations

from typing import NamedTuple

import plans.errors as errors
import plans.service as service

#: The close reason and detach reason every replacement records.
REPLACED = "replaced"


class Replacement(NamedTuple):
    """The attached step, and every thread the replace cut off from the parent."""

    node: service.PlanNode
    replaced: str | None
    stranded: list[str]


def replace_spawned_thread(
    node_id: str,
    spawned_thread_id: str,
    *,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
) -> Replacement:
    """Move a step's attachment, ending the old worker's part; name who it cut off.

    A node with no worker, or already on this one, is simply attached. Any
    refusal from the attach (a replacement this plan's owner did not spawn,
    one attached elsewhere, a cross-project thread) rolls the whole write back,
    so the old worker is never left closed with nobody attached. ``replaced``
    is the old worker; ``stranded`` its own workers whose steps this closed.
    """
    from plans import delegation, delegation_child_lifecycle, delegation_harvest, plan_events

    replaced: str | None = None
    stranded: list[str] = []
    with service._connection(db) as connection, service._write(connection):
        node = service._live_node_row(connection, node_id)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        old = node["spawned_thread_id"]
        if old is not None and old != spawned_thread_id:
            replaced = str(old)
            stranded = delegation_child_lifecycle.end_open_work(
                connection, old, reason=REPLACED,
                actor_id=actor_id, provenance=provenance,
            )
            delegation.detach_spawned_thread(
                node_id, reason=REPLACED, actor_id=actor_id,
                provenance=provenance, db=connection,
            )
            plan_events.append_worker_replaced(
                connection, worker_id=replaced, node_id=node_id,
                replacement_id=spawned_thread_id, actor_id=actor_id,
                created_at=service._now(),
            )
            if delegation_harvest.awaiting_owner_verdict(connection, node):
                service.transition_node(
                    node_id, "active", actor_id=actor_id,
                    provenance=provenance, db=connection,
                )
        attached = delegation.attach_spawned_thread(
            node_id, spawned_thread_id, actor_id=actor_id,
            provenance=provenance, db=connection,
        )
    return Replacement(attached, replaced, stranded)
