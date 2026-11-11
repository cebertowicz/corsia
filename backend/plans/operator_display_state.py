"""Canonical four-state display vocabulary and pure node reducers."""

from __future__ import annotations


_HUMAN_REVIEWS = frozenset({"human_decision", "human_review"})

#: The exact display vocabulary the backend can emit. The client mirrors these
#: in ``plansTypes.ts`` and parity tests keep both sides synchronized.
NODE_LABEL_KEYS = frozenset({"working", "needs_you", "needs_agent_input", "waiting", "finished"})
#: A worker's input need is a step's wait; a plan never reads it.
PLAN_LABEL_KEYS = NODE_LABEL_KEYS - {"needs_agent_input"}
TONE_KEYS = frozenset({
    "active", "human-review", "waiting", "complete", "failed", "closed",
})

#: What each terminal state reads as. One table keeps live and retired reads
#: from disagreeing about an outcome.
TERMINAL_DISPLAY = {
    "accepted": {"label_key": "finished", "tone_key": "complete"},
    "accepted_with_followup": {"label_key": "finished", "tone_key": "complete"},
    "failed": {"label_key": "finished", "tone_key": "failed"},
    "closed": {"label_key": "finished", "tone_key": "closed"},
}

_ARCHIVED_UNSETTLED = {"label_key": "finished", "tone_key": "closed"}


def retired_display(state: str) -> dict[str, str]:
    """Return the display for an archived node from its durable state alone."""
    return dict(TERMINAL_DISPLAY.get(state, _ARCHIVED_UNSETTLED))


def review_kind_from_facts(
    state: str, assignee: str | None, owner_kind: str, *, human_decision: bool,
) -> str | None:
    """Canonical reviewer kind before presentation/actor resolution."""
    if state != "review_requested":
        return None
    if human_decision:
        return "human_decision"
    return "human_review" if assignee == "user" or owner_kind != "thread" else "parent_agent"


def plan_defers_asks(plan_state: str) -> bool:
    """A paused plan defers every ask it owns on the person until resume (``ask_deferred``).

    Admission still reads a raised review or decision as such and the person can
    still answer it; only the attention it raises (needs-you marks, counts, the
    dock) is deferred. Independent host-access requests are not plan-owned.
    """
    return plan_state == "paused"


def plan_paused(execution: dict) -> bool:
    """``plan_defers_asks`` for a node row: a paused plan puts this cause on every row."""
    return any(cause.get("reason_key") == "plan_paused" for cause in execution["causes"])


def node_paused(execution: dict) -> bool:
    """Whether this row carries the user-controlled pause from steering facts."""
    from plans import user_steering

    pause = user_steering.placed_pause(execution["causes"])
    return pause is not None and user_steering.is_user_pause(pause)


def ask_deferred(*, plan_paused: bool = False, node_paused: bool = False, covered: bool = False) -> bool:
    """The one rule for suppressing a plan-owned ask's attention until resume."""
    return plan_paused or node_paused or covered


def review_deferred(
    condition: str, review_kind: str | None, *, plan_paused: bool, node_paused: bool,
) -> bool:
    """Display and bucket consequence of ``ask_deferred`` for a row in human review."""
    return (
        ask_deferred(plan_paused=plan_paused, node_paused=node_paused)
        and condition == "review" and review_kind in _HUMAN_REVIEWS
    )


def node_paused_sql(node_id_sql: str) -> str:
    """SQL twin of ``node_paused``: the node carries an uncleared user pause hold."""
    return f"""EXISTS (
        SELECT 1 FROM plan_node_holds pause
         WHERE pause.node_id={node_id_sql} AND pause.cleared_at IS NULL
           AND pause.kind='paused' AND pause.owner_kind='user'
    )"""


def covered_sql(node_id_sql: str) -> str:
    """Whether a comment task is covered by another live ask on its document."""
    from plans import node_states, user_ask

    return f"""EXISTS (
        SELECT 1 FROM plan_node_artifact_links comment_link
        JOIN plan_nodes comment_node ON comment_node.id=comment_link.node_id
        JOIN plans comment_plan ON comment_plan.id=comment_node.plan_id
        JOIN threads comment_owner ON comment_owner.id=comment_plan.owner_id
        JOIN plan_node_artifact_links other_link ON other_link.artifact_id=comment_link.artifact_id
        JOIN plan_nodes other_node ON other_node.id=other_link.node_id
        JOIN plans other_plan ON other_plan.id=other_node.plan_id
        JOIN threads other_owner ON other_owner.id=other_plan.owner_id
        WHERE comment_link.node_id={node_id_sql}
          AND comment_link.provenance='document.person_comment_task'
          AND other_link.node_id<>comment_link.node_id
          AND COALESCE(other_link.provenance,'')<>'document.person_comment_task'
          AND comment_owner.user_id=other_owner.user_id
          AND {node_states.open_sql('other_node')}
          AND other_plan.state<>'paused' AND NOT {node_paused_sql('other_node.id')}
          AND ((other_link.role='input' AND other_node.kind='task' AND other_node.assignee='user')
            OR (other_link.role='attention' AND EXISTS (
                SELECT 1 FROM plan_node_holds h
                LEFT JOIN plan_ask_deferrals d ON d.hold_id=h.id
                WHERE h.node_id=other_node.id AND h.owner_id=other_link.artifact_id
                  AND {user_ask.open_user_block_sql('h')} AND d.hold_id IS NULL
            )))
    )"""


def covered_nodes(node_ids, *, db) -> set[str]:
    """Read coverage once for a batch of projected nodes."""
    import json

    if not node_ids:
        return set()
    rows = db.execute(
        f"SELECT n.id FROM plan_nodes n WHERE n.id IN (SELECT value FROM json_each(?)) "
        f"AND {covered_sql('n.id')}", (json.dumps(list(node_ids)),),
    )
    return {str(row['id']) for row in rows}


def ask_deferred_sql(node_id_sql: str, *, plan_state_sql: str) -> str:
    """SQL twin of ``ask_deferred`` for a linked plan node and its plan."""
    return f"({plan_state_sql}='paused' OR {node_paused_sql(node_id_sql)} OR {covered_sql(node_id_sql)})"


def asks_user_from_facts(
    state: str, condition: str, assignee: str | None, review_kind: str | None,
    *, has_attention: bool, causes: list[dict],
) -> bool:
    """Whether the row asks the person, independently of a pause ``ask_deferred`` counts."""
    from plans import user_steering

    pause_only = bool(causes) and all(
        cause.get("reason_key") == "plan_paused"
        or (cause.get("placed") and user_steering.is_user_pause(cause))
        for cause in causes
    )
    return state not in TERMINAL_DISPLAY and bool(
        (assignee == "user" and (condition == "ready" or (condition == "paused" and pause_only)))
        or review_kind in _HUMAN_REVIEWS or has_attention
    )


def row_asks_user(node: dict, review: dict | None, execution: dict) -> bool:
    """``asks_user_from_facts`` for a full node row: its look in the plan.

    The ask is owed now (``requires_user_action``) only while ``ask_deferred`` is false.
    """
    return asks_user_from_facts(
        str(node["state"]), str(execution["condition"]),
        None if node.get("assignee") is None else str(node["assignee"]),
        None if review is None else str(review["kind"]),
        has_attention=any(
            link["role"] == "attention" and link.get("attention_state") == "raised"
            for link in node.get("artifact_links", [])
        ),
        causes=execution["causes"],
    )


def display_from_facts(
    state: str,
    condition: str,
    review_kind: str | None = None,
    *,
    asks_user: bool = False,
    child_working: bool = False,
    executor_stopped: bool = False,
    worker_needs_input: bool = False,
) -> dict[str, str]:
    """Reduce compact lifecycle facts into the canonical four-state display.

    ``asks_user`` is ``asks_user_from_facts``: a pause defers an ask's attention, never its look.
    """
    if state in TERMINAL_DISPLAY:
        return dict(TERMINAL_DISPLAY[state])
    if asks_user:
        return {"label_key": "needs_you", "tone_key": "human-review"}
    if worker_needs_input:
        return {"label_key": "needs_agent_input", "tone_key": "waiting"}
    if condition == "review":
        kind = review_kind or "parent_agent"
        return {
            "label_key": "waiting" if kind == "external" else "working",
            "tone_key": "waiting" if kind == "external" else "active",
        }
    if executor_stopped and condition == "running":
        return {"label_key": "waiting", "tone_key": "waiting"}
    if condition == "running" or child_working:
        return {"label_key": "working", "tone_key": "active"}
    if condition in {"waiting", "paused", "blocked", "ready"}:
        # Ready work nobody is moving waits its turn: an agent moves one task
        # at a time, so Working means moving (owner, 2026-09-30).
        return {"label_key": "waiting", "tone_key": "waiting"}
    return {"label_key": "working", "tone_key": "active"}


def queued_reason(node: dict) -> str | None:
    """Why a presented leaf that nothing blocks is not moving, or ``None``."""
    if node["display"]["label_key"] != "waiting" or node["execution"]["condition"] != "ready":
        return None
    if node["child"] is not None:
        return "Worker idle"
    return "Not started" if node["state"] == "pending" else "Set aside"
