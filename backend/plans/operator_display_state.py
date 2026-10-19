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


def ask_deferred(*, plan_paused: bool = False, node_paused: bool = False) -> bool:
    """The one rule for suppressing a plan-owned ask's attention until resume."""
    return plan_paused or node_paused


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


def ask_deferred_sql(node_id_sql: str, *, plan_state_sql: str) -> str:
    """SQL twin of ``ask_deferred`` for a linked plan node and its plan."""
    return f"({plan_state_sql}='paused' OR {node_paused_sql(node_id_sql)})"


def needs_user_from_facts(
    state: str,
    condition: str,
    assignee: str | None,
    review_kind: str | None,
    *,
    has_attention: bool,
    plan_paused: bool = False,
    node_paused: bool = False,
) -> bool:
    """Return whether compact node facts expose a concrete user action now.

    Every plan-owned ask is deferred under a plan or item pause (``ask_deferred``).
    """
    if state in TERMINAL_DISPLAY or ask_deferred(plan_paused=plan_paused, node_paused=node_paused):
        return False
    return bool(
        (assignee == "user" and condition == "ready")
        or review_kind in _HUMAN_REVIEWS
        or has_attention
    )


def display_from_facts(
    state: str,
    condition: str,
    review_kind: str | None = None,
    *,
    needs_user: bool = False,
    child_working: bool = False,
    executor_stopped: bool = False,
    worker_needs_input: bool = False,
    plan_paused: bool = False,
    node_paused: bool = False,
) -> dict[str, str]:
    """Reduce compact lifecycle facts into the canonical four-state display."""
    if state in TERMINAL_DISPLAY:
        return dict(TERMINAL_DISPLAY[state])
    if needs_user:
        return {"label_key": "needs_you", "tone_key": "human-review"}
    if worker_needs_input:
        return {"label_key": "needs_agent_input", "tone_key": "waiting"}
    if review_deferred(condition, review_kind, plan_paused=plan_paused, node_paused=node_paused):
        return {"label_key": "waiting", "tone_key": "waiting"}
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
