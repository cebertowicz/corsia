"""The community card CORSIA raises itself when first-run step 7 becomes current, so no model has to.

Step 7 of the app path ("How CORSIA gets better") asks the community decision.
Copying a long payload into three exact tool calls is the part small local models
get wrong, and the payload is code-built, so code raises it. Plans schedules
``raise_for_current_step`` after the commit that selected the step; the answer side
of the same card is ``infra/community_answer.py``.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

WORKFLOW = "first-run-app"
COMMUNITY_STEP = "7:common"
_PROVENANCE = "infra.community_card"
_FIELDS = ("title", "context", "tradeoff", "recommendation", "rationale", "options", "other_enabled")


def raise_for_current_step(node_id: str, plan_id: str, *, db=None) -> None:
    """Raise the community card when step 7 is current; never raises, never repeats.

    Plans calls it only for step 7. Acts only in the CORSIA agent's own plan, because the
    profile write after the answer needs that owner, and never when the step already has a decision.
    """
    from infra.database import get_db

    connection = db or get_db()
    try:
        _raise_community(connection, node_id, plan_id)
    except Exception:  # noqa: BLE001 - a card is never worth failing the plan write
        log.warning("community card not raised for step %s", node_id, exc_info=True)
    finally:
        if db is None:
            connection.close()


def _raise_community(db, node_id: str, plan_id: str) -> None:
    from infra.corsia_agent import get_corsia_agent
    from infra.database import write_transaction
    from onboarding import setup_decisions
    from plans import public

    owner = public.decision_owner_id(db, node_id=node_id)
    if owner is None or get_corsia_agent(db=db) != owner:
        return
    if public.node_row(db, node_id)["state"] != "active" or public.has_decision_from(db, node_id=node_id):
        return
    thread = db.execute("SELECT user_id, project_id FROM threads WHERE id = ?", (owner,)).fetchone()
    if thread is None:
        return
    card = setup_decisions.community_card()
    common = {
        "actor": public.PlanActor(owner, thread["user_id"], thread["project_id"]),
        "provenance": _PROVENANCE, "db": db,
    }
    key = f"community-card:{node_id}"
    results = []
    with write_transaction(db, scope="community_card"):
        drafted = public.draft_decision(
            plan_id, source_node_id=node_id, mutation_id=f"{key}:draft", **common,
            **{field: card[field] for field in _FIELDS},
        )
        results.append(drafted)
        decision = drafted.result["decision"]
        for step, operation in (("review", public.parent_review_decision), ("raise", public.raise_decision)):
            result = operation(
                decision["id"], expected_revision=decision["revision"], mutation_id=f"{key}:{step}", **common,
            )
            results.append(result)
            decision = result.result["decision"]
    for result in results:
        public.deliver_deferred(result)
