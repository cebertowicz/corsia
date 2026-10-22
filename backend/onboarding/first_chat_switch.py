"""Move the first chat from the model setup gave it to Claude Code or Codex once a new sign-in is ready.

A person can start using CORSIA on the local model or an API key while a subscription is
still installing, or sign in to one later from Settings. The first time such a sign-in
reads ready, the first chat switches to the tool, with the conversation carried over, through
the one model-switch door (``thread.model.switch_model``). The CORSIA agent is the
instance's one agent, so a Settings sign-in has no first-run bind and no user. Three guards keep it narrow:

1. only the first chat setup created: the CORSIA agent, whose first model setup recorded;
2. only if the person has not picked a model in it since: the thread still holds that
   model, in the same execution epoch, with no switch pending;
3. only while no first-chat switch is in flight: concurrent provider completions claim once;
   a failed switch releases that claim so any later sign-in completion retries.
"""

from __future__ import annotations

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
            "claimed": None,
        }, db=db)
    finally:
        db.close()


def _destination(provider: str, account_id: str, thread_id: str, catalog_rows: list[dict], *, db: sqlite3.Connection) -> str | None:
    """The first account-ready vendor model in the catalog's public presentation order."""
    from connections.account_policy import thread_project_scopes
    from connections.vendor_accounts import get_account
    from llm.account_catalog import account_model_status

    account = get_account(account_id, db=db)
    project_ids = thread_project_scopes(thread_id, None, db=db)
    for row in catalog_rows:
        if row.get("connection") != provider:
            continue
        if account_model_status(account, row, project_ids=project_ids, db=db)["status"] == "ready":
            return str(row["litellm_model"])
    return None


def claim_switch(provider: str, operation_id: str, *, account_id: str, db: sqlite3.Connection) -> tuple[str, str] | None:
    """The ``(thread_id, destination)`` to switch for this operation, claiming it; None when a guard fails."""
    from llm.model_status import catalog_base_rows

    # The catalog opens its own short-lived read handle, so take this snapshot before
    # the write transaction that must keep its nested settings write atomic.
    if not get_setting(SETTINGS_KEY, db=db):
        return None
    catalog_rows = catalog_base_rows()
    with write_transaction(db, scope="first_chat_switch_claim") as connection:
        marker = get_setting(SETTINGS_KEY, db=connection)
        if not marker or marker.get("claimed"):
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
        destination = _destination(provider, account_id, str(marker["thread_id"]), catalog_rows, db=connection)
        if destination is None:
            log.info("first chat switch skipped: no ready destination for account %s on %s", account_id, provider)
            return None
        set_setting(SETTINGS_KEY, {**marker, "claimed": operation_id}, db=connection)
        return str(marker["thread_id"]), destination


def release_claim(operation_id: str, *, db: sqlite3.Connection) -> None:
    """Release this failed in-flight claim so a later sign-in completion can retry."""
    with write_transaction(db, scope="first_chat_switch_release") as connection:
        marker = get_setting(SETTINGS_KEY, db=connection)
        if marker and marker.get("claimed") == operation_id:
            set_setting(SETTINGS_KEY, {**marker, "claimed": None}, db=connection)


async def switch_when_ready(provider: str, operation_id: str, *, user_id: str | None, account_id: str) -> bool:
    """Switch the first chat to ``provider``'s model if the three guards hold; whether a switch was asked for.

    A sign-in started from Settings has no first-run bind and so no ``user_id``; the switch door then skips its ownership check.
    """
    from infra.db_async import run_in_db
    from infra.database import get_db
    from thread.model import switch_model

    def claim() -> tuple[str, str] | None:
        db = get_db()
        try:
            return claim_switch(provider, operation_id, account_id=account_id, db=db)
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
