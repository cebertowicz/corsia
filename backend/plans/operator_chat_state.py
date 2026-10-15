"""The chat list's one word per thread, read through the operator envelope."""

from __future__ import annotations

from core.thread_identity import root_visible_thread_ids
import infra.thread_updates as thread_updates
import plans.node_states as node_states
import plans.not_moving as not_moving
import plans.operator_node_state as operator_node_state
import plans.open_rollup as open_rollup
import plans.open_tree as open_tree
import plans.operator_owners as operator_owners
import plans.operator_projection as operator_projection
import plans.service as service
from plans.boundary import PlanActor

THREAD_STATE_LIMIT = 200

# The chat list's word for each state the plan header leads with; any other
# lead (blocked, ready, finished) asks nothing of the user and reads idle. A
# plan that is only waiting is not moving (stuck plans R9).
_CHAT_WORD = {"active": "moving", "review_parent_agent": "moving", "paused": "paused"}
_OWED_STATES = frozenset({"needs_input", "blocked_on_you"})


def chat_state(
    plan: dict, counts: dict[str, int] | None, *, owed: bool = False, stuck: dict | None = None,
) -> str:
    """The chat list's one word for a thread: the plan header's lead, renamed.

    A human decision or review, or a host-access request, is the user's move
    and outranks a pause (pause contract rule 5): beside active
    work the agent still moves, paused or alone it is blocked on the user.
    Otherwise ``operator_node_state.leading``, the header's one precedence,
    decides. ``counts`` is None with nothing open. ``stuck`` is the header's
    not-moving parts (``plans.not_moving.read``): a still plan never reads
    moving; it is paused when every open leaf is the user's pause, and
    otherwise idle. Its one-tap asks already read blocked on the user above; a
    user-owned hold or a ready task assigned to the user alone never does
    (#2638, owner P0 2026-09-27).
    """
    paused = plan["state"] == "paused"
    if owed or (counts and counts["review_human"]):
        if not counts:
            return "blocked_on_you"
        return "needs_input" if counts["active"] and not paused else "blocked_on_you"
    if paused or not counts:
        return "paused" if paused else "idle"
    if stuck is not None:
        return _still_word(counts, stuck)
    return _CHAT_WORD.get(operator_node_state.leading(counts), "idle")


def _still_word(counts: dict[str, int], stuck: dict) -> str:
    return "paused" if stuck["paused_by_you"] == not_moving.still_count(counts) else "idle"


def _thread_items(rows, kind: str) -> dict[str, list[dict]]:
    """Group already-authorized row facts by plan id for one attention class."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["plan_id"]), []).append({
            "kind": kind,
            "title": row["title"],
            "node_id": row["node_id"],
        })
    return grouped


def _failed_nodes(plan_ids: list[str], *, actor: PlanActor, db) -> dict[str, list[dict]]:
    """Group failed nodes the viewer has not yet seen in that plan's pane."""
    if not plan_ids:
        return {}
    marks = ",".join("?" for _ in plan_ids)
    # A plan whose stored counters hold no failed leaf has nothing to ask. Otherwise the
    # candidates are the user's failed rows changed since the viewer last saw the plan
    # (``idx_plan_nodes_user_failed``): a failure unseen happened after ``seen_through``, and
    # a node's last change is never before its failure. Settled history is never read (#731 S2-2).
    rows = db.execute(
        f"""SELECT n.plan_id,n.id AS node_id,n.title FROM plans p
              LEFT JOIN plan_pane_views v ON v.user_id=? AND v.thread_id=p.owner_id
              CROSS JOIN plan_nodes n ON n.plan_id=p.id
              LEFT JOIN plan_node_events failure
                ON failure.node_id=n.id AND failure.event='state_transition'
               AND failure.new_state='failed'
              WHERE p.id IN ({marks}) AND p.top_failed>0
                AND n.own_last_changed>COALESCE(v.seen_through,'')
                AND {node_states.failure_asks_user_sql('n')}
                AND (failure.created_at IS NULL OR v.seen_through IS NULL
                     OR failure.created_at > v.seen_through)
              ORDER BY n.plan_id,n.order_key,n.id""",
        (actor.user_id, *plan_ids),
    ).fetchall()
    return _thread_items(rows, "failed")


def _access_requests(thread_ids: list[str], *, actor: PlanActor, db) -> dict[str, list[dict]]:
    """Group live awaiting access requests by their canonical visible root."""
    if not thread_ids:
        return {}
    rows = db.execute(
        f"""SELECT l.root_thread_id,l.purpose FROM host_access_leases l
              JOIN threads t ON t.id=l.root_thread_id
             WHERE l.state='awaiting_user' AND t.user_id=? AND t.purged_at IS NULL
               AND {operator_owners.LIVE_THREAD}
             ORDER BY l.root_thread_id,l.created_at,l.id""",
        (actor.user_id,),
    ).fetchall()
    roots = root_visible_thread_ids([str(row["root_thread_id"]) for row in rows], db=db)
    visible_ids = set(thread_ids)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        root_id = roots[str(row["root_thread_id"])]
        if root_id not in visible_ids:
            continue
        grouped.setdefault(root_id, []).append({
            "kind": "access",
            "title": row["purpose"] or "Host access",
            "node_id": None,
        })
    return grouped


def _plan_attention_items(presented: dict) -> list[dict]:
    """Every actionable plan ask, using the chat list's existing attention shape."""
    items = []
    work = node_states.work_ids(presented["nodes"])
    parents = node_states.parent_ids(presented["nodes"])
    for node in presented["nodes"]:
        if (
            node["id"] not in work or node["id"] in parents or node["folded"]
            or not node["requires_user_action"]
        ):
            continue
        review = node["review"]
        if review is None:
            kind = "hold"
        else:
            kind = "decision" if review["kind"] == "human_decision" else "review"
        items.append({"kind": kind, "title": node["title"], "node_id": node["id"]})
    return items


def _live_visible_thread_ids(ids: list[str], *, actor: PlanActor, db) -> list[str]:
    """Return requested threads which are live and visible to the operator."""
    rows = db.execute(
        f"""SELECT id FROM threads WHERE user_id = ? AND purged_at IS NULL
              AND {operator_owners.LIVE_THREAD} AND id IN ({','.join('?' * len(ids))})""",
        (actor.user_id, *ids),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _plan_word(plan_row: dict, plan: dict | None, *, owed: bool) -> tuple[str, str | None]:
    """A plan's chat word and header label; ``plan`` is None with nothing open."""
    if plan is None:
        return chat_state(plan_row, None, owed=owed), None
    display = plan["display"]
    state = chat_state(
        plan["plan"], plan["progress"]["distribution"], owed=owed, stuck=display.get("not_moving"),
    )
    return state, display["label_key"]


def _plan_rows(thread_ids: list[str], *, db) -> list:
    """Each named thread's live plan row, with whether it has open and finished work.

    Open is a row of the open work, the rule the pane lists it by
    (``node_states.listed_open_sql``, the open index); finished is a settled row in work,
    read from a folded index, so neither walks a plan's history.
    """
    terminal = sorted(service._TERMINAL_STATES)
    marks = ','.join('?' * len(terminal))
    return db.execute(
        f"""SELECT p.*, EXISTS (
                SELECT 1 FROM plan_nodes n WHERE n.plan_id = p.id
                  AND {node_states.listed_open_sql('n')}
              ) AS has_open, EXISTS (
                SELECT 1 FROM plan_nodes n WHERE n.plan_id = p.id
                  AND n.folded = 1 AND n.state IN ({marks}) AND {node_states.in_work_sql('n')}
              ) AS has_finished
            FROM plans p
            WHERE p.owner_kind = 'thread' AND p.state != 'archived'
              AND p.owner_id IN ({','.join('?' * len(thread_ids))})""",
        (*terminal, *thread_ids),
    ).fetchall()


def read_thread_states(thread_ids: list[str], *, actor: PlanActor, db) -> dict:
    """The chat-list state for the actor's named live threads.

    Open plans use the operator envelope. Every row carries ``has_update`` and
    ``opened`` even without a plan; an absent row is idle, never opened, with no
    update. ``settled`` is the plan's not-paused, no-open-work fact, so the
    client must also check its live turn truth. AS3 is owed > Failed > dot.
    """
    operator_projection._require_human(actor)
    ids = list(dict.fromkeys(thread_ids))
    if len(ids) > THREAD_STATE_LIMIT:
        raise ValueError(f"read at most {THREAD_STATE_LIMIT} threads at once; {len(ids)} were named")
    if not ids:
        return {"states": {}}
    visible_thread_ids = _live_visible_thread_ids(ids, actor=actor, db=db)
    if not visible_thread_ids:
        return {"states": {}}
    rows = _plan_rows(visible_thread_ids, db=db)
    states: dict[str, dict] = {}
    plans_by_id = {str(row["id"]): dict(row) for row in rows}
    memo: dict = {}
    # No folded rows are listed (cap 0): the header counts come from the stored counters.
    projected = open_rollup.rollup_plans(
        db, [row for row in rows if row["has_open"]], memo,
        window=open_tree.FoldWindow(since=None, cap=0),
    )
    # The chat list reads the plan header and the ask list only: no costs, no
    # attempt, evidence or transition facts, and none of the envelope's
    # decision count or revision token.
    presented = operator_projection.presented_plans(
        projected, actor, db=db, costs=False, memo=memo, lean=True,
    )
    presented_by_id = {str(plan["plan"]["id"]): plan for plan in presented}
    plan_ids = list(plans_by_id)
    failed_by_plan = _failed_nodes(plan_ids, actor=actor, db=db)
    access_by_thread = _access_requests(visible_thread_ids, actor=actor, db=db)
    latest_updates, attention_updates = thread_updates.latest_by_thread_with_attention(
        db, visible_thread_ids,
    )
    seen = thread_updates.seen_by_thread(db, actor.user_id, attention_updates, visible_thread_ids)
    for plan_id, plan_row in plans_by_id.items():
        plan = presented_by_id.get(plan_id)
        plan_items = _plan_attention_items(plan) if plan is not None else []
        user_items = [*plan_items, *access_by_thread.get(str(plan_row["owner_id"]), [])]
        state, label_key = _plan_word(plan_row, plan, owed=bool(user_items))
        # Your move outranks Failed; its list is whatever owed items were found.
        if state in _OWED_STATES:
            attention_items = user_items
        elif failed_by_plan.get(plan_id):
            state = "failed"
            attention_items = failed_by_plan[plan_id]
        else:
            attention_items = []
        states[plan_row["owner_id"]] = {
            "state": state,
            "label_key": label_key,
            "paused": plan_row["state"] == "paused",
            "attention_items": attention_items,
            "settled": bool(
                plan_row["state"] != "paused" and not plan_row["has_open"]
                and plan_row["has_finished"]
            ),
        }
    _add_planless_rows(states, access_by_thread, seen)
    for thread_id, row in states.items():
        row["latest_update"] = latest_updates.get(thread_id)
        row.update(seen.get(thread_id, _UNSEEN))
    return {"states": states}


_UNSEEN = {"has_update": False, "opened": False}


def _add_planless_rows(
    states: dict[str, dict], access_by_thread: dict[str, list[dict]], seen: dict[str, dict],
) -> None:
    """Give a thread with no plan a row when it owes access, was opened or has an update."""
    for thread_id, attention_items in access_by_thread.items():
        states.setdefault(thread_id, {
            "state": "blocked_on_you",
            "label_key": None,
            "paused": False,
            "attention_items": attention_items,
            "settled": False,
        })
    for thread_id in seen:
        states.setdefault(thread_id, {
            "state": "idle",
            "label_key": None,
            "paused": False,
            "attention_items": [],
            "settled": False,
        })
