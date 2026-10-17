"""Move the first chat from the local model to Claude Code or Codex once setup's sign-in finishes.

A person who picked a subscription in setup can start using CORSIA on the local model
while that tool is still installing. When their sign-in completes, the first chat
switches to the tool, with the conversation carried over, through the one model-switch
door (``thread.model.switch_model``). Three guards keep it narrow:

1. only the first chat setup created: the CORSIA agent, whose first model setup recorded;
2. only if the person has not picked a model in it since: the thread still holds that
   model, in the same execution epoch, with no switch pending;
3. only once per sign-in operation: a repeated ready signal for the same operation is a no-op,
   unless its switch failed, which releases the claim so the next completion retries.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from infra.database import write_transaction
from infra.settings import get_setting, set_setting

log = logging.getLogger(__name__)

SETTINGS_KEY = "setup_first_chat_model"
# A key, like the other switch reasons (``harness_model_switch``), not a sentence.
REASON = "setup_first_chat_ready"


def record_first_model(thread_id: str, model: str) -> None:
    """Remember the model setup gave the CORSIA agent, when it is not a Claude Code or Codex one."""
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db
    from infra.vendor_turn_binding import model_vendor

    db = get_db()
    try:
        if get_corsia_agent(db=db) != thread_id or model_vendor(model, db=db) is not None:
            return
        row = db.execute("SELECT execution_epoch FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            return
        set_setting(SETTINGS_KEY, {
            "thread_id": thread_id, "model": model.strip(), "execution_epoch": int(row["execution_epoch"]),
            "handled": [],
        }, db=db)
    finally:
        db.close()


def _destination(provider: str, *, db: sqlite3.Connection) -> str | None:
    """The tool's default listed model, else its first one."""
    rows = db.execute(
        "SELECT litellm_model, metadata_json FROM models WHERE harness=? AND enabled=1 AND available=1",
        (provider,),
    ).fetchall()
    ranked = []
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            meta = {}
        ranked.append((not meta.get("is_default"), meta.get("discovery_order", 0), str(row["litellm_model"])))
    return min(ranked)[2] if ranked else None


def claim_switch(provider: str, operation_id: str, *, db: sqlite3.Connection) -> tuple[str, str] | None:
    """The ``(thread_id, destination)`` to switch for this operation, claiming it; None when a guard fails."""
    with write_transaction(db, scope="first_chat_switch_claim") as connection:
        marker = get_setting(SETTINGS_KEY, db=connection)
        if not marker or operation_id in marker.get("handled", []):
            return None
        thread = connection.execute(
            "SELECT model,pending_model,execution_epoch FROM threads WHERE id=?", (marker.get("thread_id"),),
        ).fetchone()
        if (
            thread is None
            or str(thread["model"] or "") != marker.get("model")
            or int(thread["execution_epoch"]) != marker.get("execution_epoch")
            or str(thread["pending_model"] or "").strip()
        ):
            return None
        destination = _destination(provider, db=connection)
        if destination is None:
            return None
        set_setting(SETTINGS_KEY, {**marker, "handled": [*marker.get("handled", []), operation_id]}, db=connection)
        return str(marker["thread_id"]), destination


def release_claim(operation_id: str, *, db: sqlite3.Connection) -> None:
    """Un-handle ``operation_id`` after a failed switch, so the next completion of it asks again."""
    with write_transaction(db, scope="first_chat_switch_release") as connection:
        marker = get_setting(SETTINGS_KEY, db=connection)
        if marker and operation_id in marker.get("handled", []):
            set_setting(SETTINGS_KEY, {**marker, "handled": [h for h in marker["handled"] if h != operation_id]},
                        db=connection)


async def switch_when_ready(provider: str, operation_id: str, *, user_id: str, account_id: str) -> bool:
    """Switch the first chat to ``provider``'s model if the three guards hold; whether a switch was asked for."""
    from infra.db_async import run_in_db
    from infra.database import get_db
    from thread.model import switch_model

    def claim() -> tuple[str, str] | None:
        db = get_db()
        try:
            return claim_switch(provider, operation_id, db=db)
        finally:
            db.close()

    def release() -> None:
        db = get_db()
        try:
            release_claim(operation_id, db=db)
        finally:
            db.close()

    claimed = await run_in_db(claim)
    if claimed is None:
        return False
    thread_id, destination = claimed
    try:
        await switch_model(
            thread_id, destination, user_id=user_id, actor_kind="system", actor_id=None,
            reason=REASON, checkpoint_override="target", account_id=account_id,
        )
    except Exception:  # noqa: BLE001 - the chat stays on the local model; the person can pick in the model picker
        log.error("first chat switch to %s failed; the next sign-in completion will retry", destination, exc_info=True)
        await run_in_db(release)
        return False
    return True
