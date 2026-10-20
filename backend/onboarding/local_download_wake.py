"""Deliver the setup flight's persisted terminal outcome after first-run is rendered.

The flight record is the sole store. Delivery only marks that record notified;
startup and the first-run render hook read it rather than keeping another outbox.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import re

from llm import local_setup_operation
from llm.local_setup import terminal_outcome
from llm.local_setup_results import CAUSES, setup_fix

log = logging.getLogger(__name__)
EVENT = "onboarding.local_download"
CHIP = "Your local-model download changed."
SUMMARY = (
    "A local model download that was active when setup handed off has reached a terminal "
    "state. Tell the person the outcome and one next step; do not rebind the CORSIA agent."
)
OUTCOMES = frozenset({"ready", "failed", "start_failed", "setup_failed"})
_REF = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_flush_lock = asyncio.Lock()


def _first_run_rendered(conductor_id: str) -> bool:
    """Whether setup's held first-run wake has been rendered and committed."""
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


def _payload(outcome: str, *, model: str | None, cause: object, ref: str | None, bytes_to_free: int | None) -> dict:
    detail: dict = {"outcome": outcome}
    if outcome == "ready":
        if not model:
            raise ValueError("a ready local-download outcome needs its model")
        detail["model"] = model[:512]
        return detail
    cause = str(cause) if cause in CAUSES else "other"
    detail.update(cause=cause, fix=setup_fix(cause, bytes_to_free))
    if ref and _REF.fullmatch(ref):
        detail["ref"] = ref
    if isinstance(bytes_to_free, int) and not isinstance(bytes_to_free, bool) and bytes_to_free > 0:
        detail["bytes_to_free"] = bytes_to_free
    return detail


async def flush_pending_local_download(conductor_id: str | None = None) -> bool:
    """Deliver the current terminal onboarding flight once its agent can receive it."""
    from events.delivery import deliver_to_thread
    from infra.corsia_agent import get_corsia_agent
    from infra.db_async import run_in_db

    async with _flush_lock:
        operation = local_setup_operation.read()
        if not operation or not operation.onboarding or operation.kind != "setup" or operation.notified:
            return False
        outcome = terminal_outcome(operation.result, downloaded=operation.downloaded)
        if outcome not in OUTCOMES:
            return False
        current = await run_in_db(get_corsia_agent)
        if not current or (conductor_id is not None and conductor_id != current):
            return False
        if not await run_in_db(_first_run_rendered, current):
            return False
        result = operation.result or {}
        payload = _payload(outcome, model=operation.repository, cause=result.get("cause"),
                           ref=result.get("ref"), bytes_to_free=result.get("bytes_to_free"))
        delivery_id = f"local-download:{operation.flight_id}:{outcome}"
        try:
            delivered = await deliver_to_thread(
                current, EVENT, SUMMARY, delivery_id=delivery_id, chip=CHIP,
                notice="## Local model download\n\n" + json.dumps(payload, sort_keys=True, ensure_ascii=False),
            )
        except Exception:
            log.error("Could not deliver local-download wake %s", delivery_id, exc_info=True)
            return False
        latest = local_setup_operation.read()
        if delivered and latest == operation:
            local_setup_operation.write(replace(operation, notified=True))
        return delivered


async def resume_pending_local_download() -> None:
    """Read the persisted flight on startup; never start or re-download a terminal flight."""
    await flush_pending_local_download()
