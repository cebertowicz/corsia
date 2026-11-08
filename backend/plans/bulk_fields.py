"""The bulk shape of ``edit`` and ``close_node``: which body a call has, and its cap.

Two verbs can name one node or many, and one required-parameter set cannot say
so, so the choice is enforced here rather than in the schema -- the way
``archive_node`` already does it (``plans.boundary_fields._validate_archive_target``).
Presence of ``node_ids`` picks the body, because dispatch routes on the key and
a null ``node_ids`` must be refused here rather than reach the writer.

A bulk body carries no ``expected_revision``: there is no single node whose
state the call is computed against, and asking for one revision per id would
make the one-call cleanup this exists for the many-call one it replaces. The
caller's ``mutation_id`` is what makes a retry safe instead, exactly as for a
bulk archive.
"""

from __future__ import annotations

from typing import Any

import plans.later_fields as later_fields
import plans.service as service

PlanInputError = service.PlanInputError
#: The verbs with a one-node body and a many-node body. ``archive_node`` has both
#: too and keeps its own validator beside the guards it enforces.
BULK_OPERATIONS = frozenset({"edit", "close_node"})
#: How many nodes one bulk call may change, whichever verb it uses: the bound
#: that stops a single call walking an unbounded plan inside one transaction.
#: ``plans.boundary_fields`` names this the archive cap, and every bulk verb now
#: shares it.
MAX_BULK_NODES = 200

#: Everything a bulk body may name besides ``node_ids``. A bulk verb does one
#: thing to every id it was given, so a field that restates one node has no
#: meaning here -- and silently writing the same title on two hundred tasks is
#: the failure this refuses.
_BULK_KEYS = {
    "edit": frozenset({"node_ids", "later", "parent_id", "reason"}),
    "close_node": frozenset({"node_ids", "reason"}),
}
_NOUN = {"edit": "edit", "close_node": "close"}

_IDS = f"node_ids must be a list of 1 to {MAX_BULK_NODES} plan node ids"
_NEEDS = {
    "edit": "bulk edit names at least one of later and parent_id to change",
    "close_node": "close_node requires a reason: say why this work will not be done",
}


def _alone(operation: str, extras: list[str]) -> str:
    """The archive's refusal, in this verb's words, naming what the caller passed.

    ``archive_node`` established the sentence (``_ARCHIVE_BULK_ALONE``): say
    which fields a bulk body cannot carry, then why they cannot describe it.
    """
    return (
        f"{operation} with node_ids takes no {', '.join(extras)}: a bulk "
        f"{_NOUN[operation]} has no single node those describe"
    )


def is_bulk(params: dict[str, Any]) -> bool:
    """Whether this call named the many-node body, whatever its ids then hold."""
    return "node_ids" in params


def reparent_target(params: dict[str, Any]) -> str | None:
    """Return the branch this edit names, as a node id or None for top level.

    Only shape is checked here: whether the branch exists, is a container and
    would not form a cycle is the move's own refusal (``plans.regroup``).
    """
    value = params["parent_id"]
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise PlanInputError("parent_id must be a node id, or null to move to top level")
    return value


def edit_body(params: dict[str, Any], text) -> dict[str, Any]:
    """Return the one change a bulk edit applies to every id it named.

    The two bodies share their field rules rather than restating them, so a bulk
    call cannot drift from what the same field means for one node.
    """
    changed: dict[str, Any] = {}
    if "parent_id" in params:
        changed["parent_id"] = reparent_target(params)
    if "later" in params:
        changed.update(later_fields.edit_fields(params, text))
    return changed


def validate_bulk(params: dict[str, Any], operation: str) -> None:
    """Reject a bulk body that describes one node, names no work, or is not a bounded list.

    Raises:
        PlanInputError: Always -- every refusal here is a body the caller can fix.
    """
    extras = sorted(set(params) - _BULK_KEYS[operation])
    if extras:
        raise PlanInputError(_alone(operation, extras))
    ids = params["node_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= MAX_BULK_NODES
        or any(not isinstance(value, str) or not value.strip() for value in ids)
    ):
        raise PlanInputError(_IDS)
    if not _carries_work(params, operation):
        raise PlanInputError(_NEEDS[operation])


def _carries_work(params: dict[str, Any], operation: str) -> bool:
    """Whether the bulk body says what to do to the ids it named."""
    if operation == "close_node":
        return "reason" in params
    return "later" in params or "parent_id" in params
