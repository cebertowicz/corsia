"""Change the person's own oMLX exactly as shown, and put it back when management stops.

Every write goes through the existing writers (``omlx_settings.save_global_settings``
and ``update_model_settings``); there is no second writer for someone else's oMLX.
While CORSIA manages it, each value is recorded in ``own_omlx_management`` before
the first change to it is sent. Under Use mine nothing is ever sent.
"""

from __future__ import annotations

import asyncio
from typing import Any

from llm import own_omlx_management
from llm.omlx_global_settings import (
    MANAGED_MODEL_SETTINGS, differences, fingerprint, record_observation, reports, writable_global_settings,
)
from llm.providers.omlx_settings import save_global_settings, update_model_settings


class ApplyRefused(RuntimeError):
    """The request can't be applied; ``status`` and the message are safe to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


STALE = "The settings changed since you were shown them; show the new diff and ask again"
USE_MINE = "You chose Use mine, so CORSIA changes nothing on your oMLX"


def _allowed(entries: list[dict[str, Any]]) -> bool:
    """Whether every row is one CORSIA may write now: never a key, network, folder or other model."""
    record = own_omlx_management.read()
    model = record.model.model if record is not None and record.manages and record.model else None
    writable = writable_global_settings()
    for row in entries:
        if "model" in row:
            if row["model"] != model or row["setting"] not in MANAGED_MODEL_SETTINGS:
                return False
        elif row["setting"] not in writable:
            return False
    return True


async def apply(server: dict[str, Any], supplied: str) -> dict[str, Any]:
    """Apply the fingerprinted diff to ``server``; per-row results, as the conductor reports them."""
    if await asyncio.to_thread(own_omlx_management.uses_mine):
        raise ApplyRefused(409, USE_MINE)
    entries = differences([server["id"]])
    if not entries:
        raise ApplyRefused(409, "No shown oMLX settings are available to apply")
    if not await asyncio.to_thread(_allowed, entries) or supplied != fingerprint(server["id"], entries):
        raise ApplyRefused(409, STALE)
    globals_ = {row["setting"]: row for row in entries if "model" not in row}
    models: dict[str, dict[str, dict[str, Any]]] = {}
    for row in entries:
        if "model" in row:
            models.setdefault(row["model"], {})[row["setting"]] = row
    try:
        await asyncio.to_thread(
            own_omlx_management.record_originals,
            {key: row["current"] for key, row in globals_.items()},
            {model_id: {key: row["current"] for key, row in rows.items()} for model_id, rows in models.items()},
        )
    except Exception:
        raise ApplyRefused(503, "CORSIA couldn't record your current values, so it changed nothing") from None
    cfg = {"server_id": server["id"]}
    applied: list[str] = []
    if globals_:
        saved = await save_global_settings(cfg, {key: row["recommended"] for key, row in globals_.items()})
        if not saved.get("ok"):
            raise ApplyRefused(int(saved.get("status", 503)), str(saved.get("message")))
        applied = list(saved.get("runtime_applied", []))
    models_saved = set()
    for model_id, rows in models.items():
        written = await update_model_settings(cfg, model_id, {key: row["recommended"] for key, row in rows.items()})
        if written.get("ok"):
            models_saved.add(model_id)
    verified = await record_observation(server, retain_on_failure=True)
    remaining = (differences([server["id"]]) if verified else entries) or []
    left = {(row["setting"], row.get("model")) for row in remaining}
    results = []
    for row in entries:
        model = row.get("model")
        done = (verified and (row["setting"], model) not in left and reports(server["id"], row["setting"], model)
                and (model is None or model in models_saved))
        active = done and (row["setting"] in applied if model is None else True)
        results.append({"setting": row["setting"], **({"model": model} if model else {}),
                        "saved": done, "active": active})
    return {"results": results, "verified": verified,
            "omlx_settings": {"differences": remaining, "fingerprint": fingerprint(server["id"], remaining)}}


async def stop(server: dict[str, Any] | None) -> dict[str, Any]:
    """Stop managing: no further change, then put back every recorded value.

    Each model gets back only its own values, one at a time. A restore that fails
    keeps what is still to put back (and the key it needs) for another try. Only
    a key CORSIA stored itself is removed once every value is back.
    """
    record = await asyncio.to_thread(own_omlx_management.begin_stop)
    if record is None:
        raise ApplyRefused(409, "CORSIA isn't managing your oMLX")
    if server is None:
        await asyncio.to_thread(own_omlx_management.restore_failed)
        raise ApplyRefused(503, "Start your oMLX, then try again.")
    cfg = {"server_id": server["id"]}
    global_restored = not record.originals_global or bool(
        (await save_global_settings(cfg, dict(record.originals_global))).get("ok"))
    models_restored = []
    for model_id, values in record.originals_models.items():
        written = await update_model_settings(cfg, model_id, dict(values))
        # A model the person removed from their oMLX has nothing left to put back.
        if written.get("ok") or written.get("model_missing"):
            models_restored.append(model_id)
    if not global_restored or len(models_restored) != len(record.originals_models):
        await asyncio.to_thread(own_omlx_management.restore_failed,
                                global_restored=global_restored, models_restored=tuple(models_restored))
        raise ApplyRefused(502, "CORSIA couldn't put your values back. Try again.")
    if record.key_registered:
        from infra.db_async import run_in_db
        from infra.key_store_servers import remove_server_key

        await run_in_db(remove_server_key, server["id"])
    await asyncio.to_thread(own_omlx_management.finish_stop)
    await record_observation(server)
    return {"ok": True}
