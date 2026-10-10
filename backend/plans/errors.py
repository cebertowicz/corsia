"""The plan mutation error taxonomy, owned in one dependency-neutral place.

Three refusal classes, distinguished by what the caller must do next:

* :class:`RevisionConflict` — the caller's revision is stale. The one refusal
  a re-read fixes: retry with the current revision it carries.
* :class:`PlanInputError` — the request is malformed and will stay malformed.
  A missing field, a wrong type, an unknown operation. Re-reading changes
  nothing; the caller must fix the body. :class:`PlanRefusal` is the member
  for a rule that refuses this actor, and its message names the route.
* Any other :class:`PlanError` — the request does not fit the plan as it is:
  an illegal transition, an unmet precondition, a plan that does not exist
  (:class:`UnknownPlan`). Resending it unchanged fails again; the caller
  changes the request.

Telling a caller holding a request that will keep failing to re-read and
retry loops it forever, which is the failure this taxonomy exists to prevent.

This module imports nothing from ``plans``, so no writer can create an import
cycle by raising from it, and ``plans.service`` re-exports ``PlanError``,
``PlanInputError`` and ``PlanRefusal`` so every existing ``service.PlanError``
reference keeps working unchanged.
"""

from __future__ import annotations


class PlanError(ValueError):
    """Raised when a requested plan mutation violates a durable invariant."""


class RevisionConflict(PlanError):
    """Raised when the caller's revision is stale: the one refusal a re-read fixes."""


class UnknownPlan(PlanError):
    """Raised when a plan id names no plan, so a boundary can name its own route."""


class PlanInputError(PlanError):
    """Raised when the request itself is malformed, so re-reading cannot help.

    A subclass, deliberately: every ``except PlanError`` site keeps its current
    behaviour, and only callers that care about the distinction opt in.
    """


class PlanRefusal(PlanInputError):
    """Raised when a rule refuses this actor; re-reading or resending cannot help.

    An input-class member, like a lane-owned node: nothing moved, and one class
    keeps both caller surfaces in step. The message is the whole answer: it
    says why, and what the actor does instead.
    """


def revision_conflict(
    subject: str, subject_id: object, expected: object, current: object,
) -> RevisionConflict:
    """Return the one refusal every stale revision raises.

    A conflict tells the caller to retry with fresh evidence, so the refusal
    carries that evidence: the revision it sent, the revision the row holds
    now, and which row. Without the current revision every retry needs a
    re-read first, which is the loop this message exists to end.
    """
    noun = subject.rsplit(" ", 1)[-1]
    named = "" if subject_id is None else f" ({noun} {subject_id})"
    return RevisionConflict(
        f"{subject} revision conflict: expected {expected}, "
        f"current {current}{named}"
    )


def open_descendants(count: object) -> PlanError:
    """The refusal for failing a node whose descendants are still open work."""
    return PlanError(
        f"plan node has {count} open descendant node(s) and cannot be failed over them: "
        "settle or close the open descendants first (close_node, accept or fail each), "
        "then fail this node"
    )


def require_node_revision(node, expected: object) -> None:
    """Refuse a node write computed against a superseded node revision."""
    if node["revision"] != expected:
        raise revision_conflict("plan node", node["id"], expected, node["revision"])


def illegal_transition(old_state: str, new_state: str, legal) -> PlanError:
    """Return the refusal for a state move the node's lifecycle does not allow.

    Naming the states the node can still move to turns a dead end into the
    next call: a caller that asked to accept active work learns it must
    request review first, from the refusal alone.
    """
    reachable = ", ".join(sorted(legal))
    return PlanError(
        f"illegal plan node transition: {old_state} -> {new_state}; "
        + (f"from {old_state} the node may move to: {reachable}"
           if reachable else f"{old_state} is terminal")
    )
