"""Keep a just-downloaded local model: pin it within the room for pins, then load it when it fits now.

oMLX's own ``is_pinned`` is the only pin record. A pinned model loads when oMLX
starts and is never unloaded to make room for another, so CORSIA keeps no pin
list. Setup's download and Add model's download both finish here.

Everything a row says about keeping a model loaded is derived live by
``host_resources.annotate_keep_loaded`` (pins full, not loaded because it doesn't
fit now), except one thing that can't be derived: that this keep attempt failed.
That event lives in ``_FAILED``, an in-memory map keyed by (server id, model id),
written only by ``keep_loaded`` and read on every model list:

| Event | Transition | Row line |
|---|---|---|
| oMLX gave no usable size | set NOT_KEPT | NOT_KEPT |
| pin write refused | set NOT_KEPT | NOT_KEPT |
| pins full | none (derived live) | pins-full line |
| doesn't fit now | none (derived live) | not-loaded line, if pinned |
| load refused, or the memory check or load raised | set NOT_LOADED | NOT_LOADED |
| person turns the switch, loads or loads anyway | cleared (``forget_failure``) | live verdicts only |
| CORSIA restarts | cleared (memory) | live verdicts only: oMLX reloads pins at start |
"""

from __future__ import annotations

import logging

from infra.host_resources import GIB, check_can_load, pin_fits, pin_room_bytes
from llm.local_model import LocalModel

log = logging.getLogger(__name__)

NOT_KEPT = "Downloaded, but couldn't keep it loaded. Turn it on to try again."
NOT_LOADED = "Not loaded: oMLX couldn't load it. Turn it on to try again."

_FAILED: dict[tuple[str, str], str] = {}


def failures(server_id: str) -> dict[str, str]:
    """The line for each model on ``server_id`` whose last keep attempt failed, by model id."""
    return {model: line for (server, model), line in _FAILED.items() if server == server_id}


def forget_failure(server_id: str, model_id: str) -> None:
    """The person acted on the row: only the live verdicts describe it now."""
    _FAILED.pop((server_id, model_id), None)


async def _model_rows(server_id: str) -> list[dict] | None:
    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    try:
        response = await admin_request({"server_id": server_id}, "GET", "/models", timeout=15.0)
        payload = response.json() if response.status_code == 200 else None
    except (OmlxAdminError, ValueError):
        return None
    rows = payload.get("models") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None


def _pinned_bytes(rows: list[dict] | None, skip: str | None = None) -> int:
    return sum(int(item.get("estimated_size") or 0) for item in rows or []
               if item.get("pinned") and item.get("id") != skip)


async def annotate_download_will_pin(sources: list[dict]) -> None:
    """Say on a running Settings download whether it will be pinned when it finishes.

    The download row's switch reads this ``will_pin``: the same pin rule ``keep_loaded`` applies
    once the bytes are on disk. Left unset when oMLX can't be asked.
    """
    from infra.db_async import run_in_db
    from llm.local_sources import corsia_omlx_server

    omlx = next((source for source in sources if source.get("source") == "omlx"), None)
    download = (omlx or {}).get("settings_download") or {}
    size = download.get("size_bytes")
    if not size or download.get("failed"):
        return
    server = await run_in_db(corsia_omlx_server)
    rows = await _model_rows(server["id"]) if server and server.get("status") == "online" else None
    if rows is not None:
        download["will_pin"] = pin_fits(int(size), _pinned_bytes(rows), pin_room_bytes())


async def keep_loaded(server_id: str, model: LocalModel) -> None:
    """Pin a completed download when it fits the room for pins, then load it when it fits now.

    Every outcome is a row state, never a failure: a pin that doesn't fit leaves the
    model downloaded and unpinned, and the row's live verdict says why; a step that
    failed is recorded for the row (see the module table). A completed download is
    never undone here.
    """
    from llm.network_discovery import discover_and_notify, notify_cached_model_loaded
    from llm.providers.omlx_lifecycle import load_model
    from llm.providers.omlx_settings import update_model_settings

    key = (server_id, model.model)
    _FAILED.pop(key, None)
    rows = await _model_rows(server_id)
    row = next((item for item in rows or [] if item.get("id") == model.model), None)
    size = row.get("estimated_size") if row else None
    if not isinstance(size, (int, float)) or size <= 0:
        log.warning("Not keeping the downloaded local model loaded: oMLX gave no usable size")
        _FAILED[key] = NOT_KEPT
        return
    if pin_fits(int(size), _pinned_bytes(rows, skip=model.model), pin_room_bytes()):
        pin = await update_model_settings({"server_id": server_id}, model.model, {"pinned": True})
        if not pin.get("ok"):
            log.warning("Couldn't pin the downloaded local model")
            _FAILED[key] = NOT_KEPT
            return
    step = "memory check"
    try:
        decision = await check_can_load(float(size) / GIB, "omlx")
        if not decision.get("ok"):
            log.warning("Not loading the downloaded local model now: not enough free memory")
            return
        step = "model load"
        result = await load_model({"server_id": server_id}, model.model)
        if not result.get("ok"):
            log.warning("Couldn't load the downloaded local model")
            _FAILED[key] = NOT_LOADED
            return
        notify_cached_model_loaded(server_id, model.model)
        step = "catalog refresh"
        await discover_and_notify(server_id)
    except Exception:
        log.warning("Not loading the downloaded local model after %s failed", step, exc_info=True)
        if step != "catalog refresh":
            _FAILED[key] = NOT_LOADED
