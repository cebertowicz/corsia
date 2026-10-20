"""Raise the oMLX settings decision again once the model CORSIA set up is in the person's oMLX.

Under "Let CORSIA manage it", the first chat can list only the oMLX-wide
settings while the picked model downloads. Once a discovery first reads that
model's settings, and when a managed setup flight ends (Settings' "Let CORSIA
manage it" comes after the first chat), this wakes the CORSIA agent with the
decision rebuilt from fresh facts, so the person sees every setting as current
→ new before anything changes. A first-run wake still waiting for its model
needs nothing: it reads fresh facts when it is delivered.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

EVENT = "onboarding.omlx_settings"
CHIP = "Your local model is ready in your oMLX."
SUMMARY = (
    "The model CORSIA set up in the person's own oMLX is ready, so its settings can be "
    "changed too. Raise the decision below unchanged from the task it names (or your "
    "current task), then on A apply it per conductor-bootstrap's references/omlx-setting.md; "
    "on B post `You left it as it is.`"
)


def _delivered_first_run(conductor_id: str) -> bool:
    """Whether the first-run wake exists and a model has already read it."""
    from infra.database import get_db
    from infra.onboarding_events import first_run_message_id, held_first_run_path

    db = get_db()
    try:
        exists = db.execute(
            "SELECT 1 FROM messages WHERE id=? AND thread_id=? LIMIT 1",
            (first_run_message_id(conductor_id), conductor_id),
        ).fetchone() is not None
        return exists and held_first_run_path(conductor_id, db=db) is None
    finally:
        db.close()


async def managed_decision() -> dict[str, Any] | None:
    """The oMLX decision from fresh facts, only while CORSIA manages the person's oMLX."""
    from onboarding import setup_decisions

    facts = await setup_decisions.read_facts(self_check_passed=True, probe_web_search=False)
    return setup_decisions.omlx_decision(facts) if facts.omlx_managed else None


async def rearm() -> bool:
    """Wake the CORSIA agent with the managed decision; whether one was delivered.

    The delivery id is the decision itself, so the same decision is raised once.
    It never raises: a decision not raised now is raised by a later discovery.
    """
    try:
        return await _rearm()
    except Exception:  # noqa: BLE001 - setup and discovery go on without the wake
        log.warning("Could not raise the oMLX settings decision again", exc_info=True)
        return False


async def _rearm() -> bool:
    from events.delivery import deliver_to_thread
    from infra.corsia_agent import get_corsia_agent
    from infra.db_async import run_in_db

    conductor_id = await run_in_db(get_corsia_agent)
    if not conductor_id or not await run_in_db(_delivered_first_run, conductor_id):
        return False
    decision = await managed_decision()
    if decision is None:
        return False
    identity = hashlib.sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()[:24]
    notice = "## oMLX settings decision\n\n" + json.dumps(decision, sort_keys=True, ensure_ascii=False)
    return await deliver_to_thread(
        conductor_id, EVENT, SUMMARY, delivery_id=f"omlx-settings:{conductor_id}:{identity}",
        chip=CHIP, notice=notice,
    )
