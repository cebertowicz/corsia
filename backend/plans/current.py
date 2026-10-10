"""Actor-bound current structured-plan read and ensure projections.

The whole-plan read carries every unarchived node's identity, state, revision,
hierarchy and ownership -- never its description or acceptance criteria, which
``plan_read(node_id=...)`` returns -- in canonical depth-first order, cut into
pages of at most ``PAGE_BYTES`` as the tool emits them. A continuation cursor
is bound to the plan revision, which every node, dependency and link write
moves, so a page after any change is refused as stale instead of mixing two
plan states. ``known_revision`` lets a caller that already holds the current
revision skip a read entirely; nothing about the caller is persisted.

A branch with no open work left in it is *folded* (``plans.node_states``):
left out of the page by default and counted per level under ``folded``, with
the exact read that includes it. The pane folds the same set by the same rule.
What folds changes only with a node write, which moves the revision, so a
cursor never mixes two folded sets; it keeps its first page's fold choice.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import plans.service as service
from plans import node_order, status_cursor
from plans.boundary import PlanActor
from plans.close_next_bounds import json_bytes
from plans.errors import PlanError, PlanInputError
from plans.node_states import folded_ids

#: Serialized bytes of one whole-plan page, measured as the tool emits it.
PAGE_BYTES = 16 * 1024

#: Never in a whole-plan page. Measured on a large real plan, these two fields
#: were 81.8% of the projection's bytes (acceptance_criteria 41.0%, description
#: 40.8%) while carrying no identity a mutation needs.
_BODY_FIELDS = (
    "description", "acceptance_criteria", "workflow_ref", "workflow_version",
    "workflow_step", "difficulty",
)

KNOWN_REVISION_NARROWED = (
    "known_revision skips an unchanged plan read; omit it when reading one "
    "node_id or continuing a cursor"
)
#: The plan revision counts node, dependency and artifact-link writes. Selection
#: and release write ``plan_node_attempts`` alone, so an unchanged revision does
#: not prove the status is unchanged: the current task can have been released
#: since. It does prove the node page is unchanged, which is what it answers.
KNOWN_REVISION_STATUS = (
    "known_revision answers mode='full' only; the status read always reads, "
    "because a task can be selected or released without changing the plan "
    "revision"
)
INCLUDE_FOLDED_FULL_ONLY = (
    "include_folded answers mode='full' only; the status read never lists "
    "folded work, and plan_read(node_id=...) reads any node, folded or not"
)
INCLUDE_FOLDED_CURSOR = (
    "this cursor continues a read cut with include_folded={cut}; pass its "
    "continue_read exactly"
)
INCLUDE_FOLDED_READ = "plan_read(mode='full', include_folded=true)"
FOLD_RULE = "finished, with no open work beneath it"


def read_current_plan(
    *,
    actor: PlanActor,
    cursor: str | None = None,
    known_revision: int | None = None,
    include_folded: bool | None = None,
    thread_id: str | None = None,
    db,
) -> dict[str, Any] | None:
    """Read one whole-plan page without creating the plan; None when not ensured.

    ``thread_id`` reads that thread's plan instead (``_owned_row``).
    """

    from plans import boundary

    actor, row = _owned_row(db, actor, thread_id)
    if row is None:
        return None
    boundary._authorize_plan_scope(db, row["id"], actor)
    unchanged = _unchanged(row, known_revision, narrowed=cursor is not None)
    return unchanged or _page(db, row, cursor, include_folded)


def read_current_status(
    *,
    actor: PlanActor,
    section: str | None = None,
    cursor: str | None = None,
    node_id: str | None = None,
    known_revision: int | None = None,
    include_folded: bool | None = None,
    thread_id: str | None = None,
    db,
) -> dict[str, Any] | None:
    """Read the compact status, one continued section, or one node exactly.

    The same owner binding as ``read_current_plan``; None when the plan is not
    ensured, so the caller can say so rather than guess.
    """

    from plans import status

    if include_folded is not None:
        raise PlanInputError(INCLUDE_FOLDED_FULL_ONLY)
    actor, row = _owned_row(db, actor, thread_id)
    if row is None:
        return None
    narrowed = node_id is not None or cursor is not None
    if known_revision is not None:
        raise PlanInputError(
            KNOWN_REVISION_NARROWED if narrowed else KNOWN_REVISION_STATUS
        )
    if node_id is not None:
        return status.node_detail(db, row, node_id, actor=actor)
    return status.read(db, row, actor=actor, section=section, cursor=cursor)


def ensure_current_plan(
    *,
    actor: PlanActor,
    cursor: str | None = None,
    known_revision: int | None = None,
    include_folded: bool | None = None,
    db,
) -> dict[str, Any]:
    """Ensure the single thread plan authorized for this actor and read one page."""

    from plans import boundary

    with service._write(db):
        actor = boundary._revalidate_actor(db, actor)
        owner_id = _resolve_owner(actor)
        plan = service.ensure_plan("thread", owner_id, db=db)
        boundary._authorize_plan_scope(db, plan.id, actor)
        row = service._plan_row(db, plan.id)
        unchanged = _unchanged(row, known_revision, narrowed=cursor is not None)
        return unchanged or _page(db, row, cursor, include_folded)


def read_checkpoint_status(*, actor: PlanActor, db) -> dict[str, Any] | None:
    """The thin plan projection a checkpoint freezes; None when there is no plan.

    The same owner binding as ``read_current_status``, delivered through
    ``plans.status_context`` so a checkpoint and the passive digest say the
    same thing about the plan.
    """

    from plans import status_context

    actor, row = _owned_row(db, actor)
    if row is None:
        return None
    return status_context.projection(db, row, actor=actor)


#: The follow-up reads a plan projection offers, each written as its owner calls it.
_READ_KEYS = frozenset({"detail_read", "continue_read", "full_read", "status_read"})


def addressed_to(result: dict[str, Any] | None, thread_id: str, *, db) -> dict[str, Any]:
    """Address a read of another thread's plan to that plan, and say whose it is.

    Every follow-up read a projection offers is written as the owner would call
    it, so a reader following one verbatim would read its own plan instead;
    each gains ``thread_id``. ``owner`` names the thread. A thread with no plan
    answers ``plan: null`` with the same owner: distinct from a refusal, and
    never an empty success.
    """
    from plans.operator_node_facts import executor_labels

    label = executor_labels({thread_id}, db=db).get(thread_id, "Untitled thread")
    owner = {"thread_id": thread_id, "label": label}
    if result is None:
        return {"plan": None, "owner": owner}
    return {**_readdress(result, thread_id), "owner": owner}


def _readdress(value, thread_id: str):
    if isinstance(value, list):
        return [_readdress(item, thread_id) for item in value]
    if not isinstance(value, dict):
        return value
    readdressed = {}
    for key, item in value.items():
        if key in _READ_KEYS and isinstance(item, str) and item.startswith("plan_read("):
            rest = item[len("plan_read("):]
            item = f"plan_read(thread_id='{thread_id}'{'' if rest == ')' else ', '}{rest}"
        readdressed[key] = _readdress(item, thread_id)
    return readdressed


def _owned_row(db, actor: PlanActor, thread_id: str | None = None):
    """Return the revalidated actor and a thread plan row, if any.

    The acting thread's own plan by default. ``thread_id`` names another
    thread's plan, readable under the one thread-plan access rule
    (``boundary.authorize_thread_owner``), checked before anything about the
    plan is looked up, so a refusal and a missing plan never tell apart a
    thread the caller may not see. Reading never creates a plan.
    """

    from plans import boundary

    actor = boundary._revalidate_actor(db, actor)
    owner_id = _resolve_owner(actor)
    if thread_id is not None and thread_id != owner_id:
        boundary.authorize_thread_owner(db, thread_id, actor)
        owner_id = thread_id
    row = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (owner_id,),
    ).fetchone()
    return actor, row


def _resolve_owner(actor: PlanActor) -> str:
    """Bind the current-plan read to the acting thread; there is no other owner."""

    if actor.kind != "thread" or actor.thread_id is None:
        raise PermissionError("the current plan is read by its own thread")
    return actor.thread_id


def _unchanged(plan_row, known_revision: int | None, *, narrowed: bool) -> dict | None:
    """Return the whole reply when the caller already holds this revision, else None.

    A node folds only through a node write, which moves ``revision``, so an
    unchanged revision is also an unchanged folded set.

    Raises:
        PlanInputError: ``known_revision`` came with a node or continuation
            read, which a plan-level revision cannot answer.
    """
    if known_revision is None:
        return None
    if narrowed:
        raise PlanInputError(KNOWN_REVISION_NARROWED)
    if known_revision != plan_row["revision"]:
        return None
    return {
        "unchanged": True,
        "plan": {
            "id": plan_row["id"], "revision": plan_row["revision"],
            "state": plan_row["state"],
        },
    }


def _page(db, plan_row, cursor: str | None, include_folded: bool | None) -> dict[str, Any]:
    """Return one byte-capped page of body-free nodes in canonical plan order."""

    plan_id = str(plan_row["id"])
    revision = str(plan_row["revision"])
    position = status_cursor.decode(cursor, plan_id=plan_id, section=status_cursor.FULL)
    cut = _cut(position, include_folded)
    ordered = node_order.ordered_nodes(db, plan_id)
    fold = folded_ids(dict(row) for row in ordered)
    listed = ordered if cut["f"] else [row for row in ordered if row["id"] not in fold]
    total = len(listed)
    offset = _offset(position, revision, total)
    page: dict[str, Any] = {
        "plan": asdict(service._plan_from_row(plan_row)),
        "progress": asdict(service.progress(plan_id, db=db)),
        "nodes": [],
        "omitted_count": total,
        "total_count": total,
    }
    if fold and not cut["f"]:
        page["folded"] = _folded_summary(ordered, fold)
    # Size the envelope at its widest -- the largest count and cursor it can
    # carry -- so the nodes' room holds whatever this page ends up emitting.
    _continue(page, plan_id, revision, total, cut)
    room = PAGE_BYTES - json_bytes(page)
    del page["cursor"], page["continue_read"]
    marked = fold if cut["f"] else frozenset()
    page["nodes"] = _fill(db, listed[offset:], room, marked)
    page["omitted_count"] = total - offset - len(page["nodes"])
    if page["omitted_count"] > 0:
        _continue(page, plan_id, revision, offset + len(page["nodes"]), cut)
    return page


def _cut(position: dict | None, include_folded: bool | None) -> dict[str, Any]:
    """The fold choice a read is cut at: the first page's, when continuing.

    Raises:
        PlanInputError: ``include_folded`` contradicts the cursor's choice.
    """
    cut = {"f": bool(include_folded if position is None else position.get("f"))}
    if position is not None and include_folded is not None and bool(include_folded) != cut["f"]:
        raise PlanInputError(INCLUDE_FOLDED_CURSOR.format(cut=str(cut["f"]).lower()))
    return cut


def _folded_summary(rows, fold: set[str]) -> dict[str, Any]:
    """Count what the page leaves out, per level, with the read that includes it.

    A level's count is its folded rows; a folded parent's own folded children
    are inside it, so ``node_count`` can exceed the levels' sum.
    """
    levels: dict[str | None, int] = {}
    for row in rows:
        if row["id"] in fold and row["parent_id"] not in fold:
            levels[row["parent_id"]] = levels.get(row["parent_id"], 0) + 1
    return {
        "node_count": len(fold),
        "levels": [{"parent_id": parent, "count": count} for parent, count in levels.items()],
        "rule": FOLD_RULE,
        "include": INCLUDE_FOLDED_READ,
    }


def _offset(position: dict | None, revision: str, total: int) -> int:
    """Return where a continued page starts, refusing a cursor from another revision."""
    if position is None:
        return 0
    if position["t"] != revision or position["o"] >= total:
        raise PlanError(status_cursor.FULL_STALE)
    return position["o"]


def _continue(
    page: dict[str, Any], plan_id: str, revision: str, offset: int, cut: dict[str, Any],
) -> None:
    """Attach the cursor and the exact read that continues from ``offset``."""
    payload = {
        "v": 1, "p": plan_id, "s": status_cursor.FULL, "t": revision, "o": offset,
    }
    if cut["f"]:
        payload["f"] = 1
    cursor = status_cursor.encode(payload)
    include = "include_folded=true, " if cut["f"] else ""
    page["cursor"] = cursor
    page["continue_read"] = f"plan_read(mode='full', {include}cursor='{cursor}')"


def _fill(db, rows, room: int, marked) -> list[dict[str, Any]]:
    """Project nodes while they fit ``room``; the first is always kept so pages advance."""
    nodes: list[dict[str, Any]] = []
    for row in rows:
        node = _node_projection(db, row)
        if row["id"] in marked:
            node["folded"] = True
        # json.dumps separates list items with ", ".
        spent = json_bytes(node) + (2 if nodes else 0)
        if nodes and spent > room:
            break
        nodes.append(node)
        room -= spent
    return nodes


def _node_projection(db, row) -> dict[str, Any]:
    """Return one node's identity, state and hierarchy projection, without bodies."""

    node = asdict(service._node_from_row(db, row))
    for field in _BODY_FIELDS:
        del node[field]
    if node["size"] is None:
        del node["size"]
    return node
