"""The server raises the community card when first-run-app step 7 becomes the next step.

Whether the library turns on rides on the card's answer (``infra.community_answer``),
so the card is raised here, by code, rather than left to the model to remember.
Runs at the end of the CORSIA agent's Turn (``automation.turn_end_effects.record_turn_end``), so a run of closes in one Turn, such as "skip the rest", is settled first. Every write carries a
fixed id, so a repeat is a replay and changes nothing.
"""

from __future__ import annotations

import sqlite3

from onboarding import setup_decisions, setup_strings as text

_PROVENANCE = "onboarding.community_card"
_STEP = 7


def _step_node(conductor_id: str, db: sqlite3.Connection) -> dict | None:
    """The open first-run-app step 7 node, only while every earlier top-level step is settled."""
    from plans import public

    steps = [
        (int(step["workflow_step"].split(":", 1)[0]), step)
        for step in public.workflow_steps(db, thread_id=conductor_id, workflow_ref="first-run-app")
    ]
    if any(not step["settled"] for number, step in steps if number < _STEP):
        return None
    return next((step for number, step in steps if number == _STEP and not step["settled"]), None)


def _card_shown(node: dict, db: sqlite3.Connection) -> bool:
    """Whether the card already reached the person; a draft or reviewed one is still to be finished."""
    from plans import public

    states = public.decision_states(db, source_node_id=node["id"], title=text.COMMUNITY["title"])
    return any(state in ("raised", "answered") for state in states)


def raise_when_next(thread_id: str) -> bool:
    """At the CORSIA agent's Turn end, post the intro and raise the card once; True when this call raised it."""
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db
    from infra.queries import insert_message
    from infra.turn_provenance import SERVER_SCRIPTED_REASON
    from plans import public

    db = get_db()
    try:
        conductor_id = get_corsia_agent(db=db)
        if not conductor_id or conductor_id != thread_id:
            return False
        thread = db.execute(
            "SELECT user_id, project_id FROM threads WHERE id=?", (conductor_id,),
        ).fetchone()
        node = _step_node(conductor_id, db) if thread is not None else None
        if thread is None or node is None or _card_shown(node, db):
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
