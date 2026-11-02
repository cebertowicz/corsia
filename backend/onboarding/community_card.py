"""The server raises the community card when first-run-app step 7 becomes the next step.

Whether the library turns on rides on the card's answer (``infra.community_answer``),
so the card is raised here, by code, rather than left to the model to remember.
Runs after a committed close (``plans.close_next_event``). Every write carries a
fixed id, so a repeat is a replay and changes nothing.
"""

from __future__ import annotations

import sqlite3

from onboarding import setup_decisions, setup_strings as text

_PROVENANCE = "onboarding.community_card"
_STEP = 7


def _step_node(conductor_id: str, db: sqlite3.Connection) -> sqlite3.Row | None:
    """The open first-run-app step 7 node, only while every earlier top-level step is settled."""
    from plans.node_states import _TERMINAL_STATES

    rows = db.execute(
        "SELECT n.id, n.plan_id, n.state, n.workflow_step FROM plan_nodes n "
        "JOIN plans p ON p.id = n.plan_id WHERE p.owner_kind='thread' AND p.owner_id=? "
        "AND n.workflow_ref='first-run-app' AND n.parent_id IS NULL AND n.archived_at IS NULL",
        (conductor_id,),
    ).fetchall()
    steps = [(int(row["workflow_step"].split(":", 1)[0]), row) for row in rows]
    if any(row["state"] not in _TERMINAL_STATES for number, row in steps if number < _STEP):
        return None
    return next(
        (row for number, row in steps if number == _STEP and row["state"] not in _TERMINAL_STATES), None,
    )


def _card_present(node: sqlite3.Row, db: sqlite3.Connection) -> bool:
    from plans.decision_projection import rows_for_plans

    return any(
        item["source_node_id"] == node["id"] and item["title"] == text.COMMUNITY["title"]
        for item in rows_for_plans([node["plan_id"]], db=db)
    )


def raise_when_next() -> bool:
    """Post the intro and raise the card once; True when this call raised it."""
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db
    from infra.queries import insert_message
    from infra.turn_provenance import SERVER_SCRIPTED_REASON
    from plans import public

    db = get_db()
    try:
        conductor_id = get_corsia_agent(db=db)
        if not conductor_id:
            return False
        thread = db.execute(
            "SELECT user_id, project_id FROM threads WHERE id=?", (conductor_id,),
        ).fetchone()
        node = _step_node(conductor_id, db) if thread is not None else None
        if thread is None or node is None or _card_present(node, db):
            return False
        card = next(item for item in setup_decisions.build_decisions(setup_decisions.Facts())
                    if item["title"] == text.COMMUNITY["title"])
        actor = public.PlanActor(conductor_id, thread["user_id"], thread["project_id"])
        fixed = f"community-card:{conductor_id}"
        intro_id = f"{fixed}:intro"
        if db.execute("SELECT 1 FROM messages WHERE id=?", (intro_id,)).fetchone() is None:
            insert_message(
                conductor_id, "assistant", text.COMMUNITY_INTRO,
                {"for_user": True, "turn_unbound_reason": SERVER_SCRIPTED_REASON},
                msg_id=intro_id,
            )
        fields = {key: card[key] for key in (
            "title", "context", "tradeoff", "recommendation", "rationale", "options", "other_enabled")}
        common = {"actor": actor, "provenance": _PROVENANCE, "db": db}
        drafted = public.draft_decision(
            node["plan_id"], source_node_id=node["id"], mutation_id=f"{fixed}:draft", **common, **fields)
        decision = public.deliver_deferred(drafted)["decision"]
        reviewed = public.parent_review_decision(
            decision["id"], expected_revision=decision["revision"], mutation_id=f"{fixed}:review", **common)
        decision = public.deliver_deferred(reviewed)["decision"]
        raised = public.raise_decision(
            decision["id"], expected_revision=decision["revision"], mutation_id=f"{fixed}:raise", **common)
        public.deliver_deferred(raised)
        return True
    finally:
        db.close()
