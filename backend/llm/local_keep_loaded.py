"""Keep a just-downloaded local model: pin it within the room for pins, then load it when it fits now.

oMLX's own ``is_pinned`` is the only pin record. A pinned model loads when oMLX
starts and is never unloaded to make room for another, so CORSIA keeps no pin
list. Setup's download and Add model's download both finish here.

Everything a row says about keeping a model loaded is derived live by
``host_resources.annotate_keep_loaded`` (no size, pins full, not loaded because it
doesn't fit now), except one thing that can't be derived: that this keep attempt
failed. That event lives in ``_FAILED``, an in-memory map keyed by (server id,
model id), written only by ``keep_loaded`` and read on every model list. Each
step also emits oMLX's ``model`` product event, marked ``automatic``:

| Event | Transition | Row line | Product event |
|---|---|---|---|
| oMLX's model list couldn't be read | set NOT_KEPT (NOT_LOADED_UNPINNED for a reload after a restart) | that line | none |
| oMLX gave no usable size | none (derived live) | no-size line | none |
| pin written | none | live verdicts | ``model.pin`` |
| pin write refused | set NOT_KEPT | NOT_KEPT | ``model.pin`` with ``error`` |
| pins full | none (derived live) | pins-full line | none |
| doesn't fit now | none (derived live) | not-loaded line, if pinned | none |
| loaded | none | live verdicts | ``model.load.completed`` |
| load refused, or the memory check or load raised | set NOT_LOADED (pinned) or NOT_LOADED_UNPINNED | that line | ``model.load.failed`` |
| person turns the switch, loads or loads anyway | cleared (``forget_failure``) | live verdicts only | the route's own |
| CORSIA restarts | cleared (memory) | live verdicts only: oMLX reloads pins at start | none |
"""

from __future__ import annotations

import logging

from infra.host_resources import GIB, check_can_load, pin_fits, pin_room_bytes
from llm.local_model_load import seat_lock
from infra.product_events import emit_product_event
from llm.local_model import LocalModel

log = logging.getLogger(__name__)

# The switch of an unpinned row is off, so turning it on retries; a pinned row's is on.
NOT_KEPT = "Downloaded, but couldn’t keep it loaded. Turn it on to try again."
NOT_LOADED = "Not loaded: oMLX couldn’t load it. Turn it off and on again to retry."
NOT_LOADED_UNPINNED = "Not loaded: oMLX couldn’t load it. Turn it on to try again."

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


async def reload_after_restart(server_id: str, model_ids: list[str]) -> None:
    """Load again, in order, each model that was loaded before oMLX exited and fits now.

    A model pinned in oMLX is loaded by oMLX itself before it answers health, so it is skipped.
    Nothing is pinned here: the person's choice of pin stays as it was.
    """
    from llm.providers.omlx_lifecycle import list_loaded

    loaded_now = {row["identifier"] for row in await list_loaded({"server_id": server_id})}
    for model_id in model_ids:
        if model_id not in loaded_now:
            model = LocalModel(repository=model_id, download_size_bytes=0, context_window=0)
            await keep_loaded(server_id, model, pin_if_fits=False)


async def keep_loaded(
    server_id: str, model: LocalModel, *, force_pin: bool = False, pin_if_fits: bool = True,
) -> None:
    """Pin a completed download, then load it when it fits now.

    Setup uses ``force_pin`` for its one managed model. Settings downloads retain
    the capacity guard so adding another model does not displace that setup model.
    Every outcome is a row state: a guarded pin that does not fit leaves the model
    downloaded and unpinned, while a failed forced pin is visible on the row. A
    completed download is never undone here. ``pin_if_fits=False`` loads without
    pinning, for a model that is already the person's.
    """
    from llm.network_discovery import discover_and_notify, notify_cached_model_loaded
    from llm.providers.omlx_lifecycle import load_model
    from llm.providers.omlx_settings import update_model_settings

    key = (server_id, model.model)
    _FAILED.pop(key, None)

    def emit(action: str, error: str | None = None) -> None:
        properties = {"provider": "omlx", "model_id": model.model, "server_id": server_id, "automatic": True}
        emit_product_event("model", action, properties={**properties, **({"error": error[:200]} if error else {})})

    rows = await _model_rows(server_id)
    row = next((item for item in rows or [] if item.get("id") == model.model), None)
    if row is None:
        log.warning("Not keeping the downloaded local model loaded: oMLX's model list couldn't be read")
        _FAILED[key] = NOT_KEPT if pin_if_fits or force_pin else NOT_LOADED_UNPINNED  # a reload was not downloaded
        return
    size = row.get("estimated_size")
    size_bytes: float | None = (
        float(size) if isinstance(size, (int, float)) and size > 0 else None
    )
    if size_bytes is None and not force_pin:
        log.warning("Not keeping the downloaded local model loaded: oMLX gave no usable size")
        return
    pinned = force_pin or (
        pin_if_fits
        and size_bytes is not None
        and pin_fits(int(size_bytes), _pinned_bytes(rows, skip=model.model), pin_room_bytes())
    )
    if pinned:
        pin = await update_model_settings({"server_id": server_id}, model.model, {"pinned": True})
        if not pin.get("ok") or pin.get("settings", {}).get("pinned") is not True:
            log.warning("oMLX did not confirm the downloaded local model as pinned")
            emit("pin", pin.get("message") or "Pin did not persist")
            _FAILED[key] = NOT_KEPT
            return
        emit("pin")
    not_loaded = NOT_LOADED if pinned else NOT_LOADED_UNPINNED
    if size_bytes is None:
        log.warning("Not loading the downloaded local model now: oMLX gave no usable size")
        return
    step = "memory check"
    try:
        # The seat's lock, so this load and a send's never check the same free memory at once.
        async with seat_lock(server_id):
            decision = await check_can_load(size_bytes / GIB, "omlx")
            if not decision.get("ok"):
                log.warning("Not loading the downloaded local model now: not enough free memory")
                return
            step = "model load"
            result = await load_model({"server_id": server_id}, model.model)
            if not result.get("ok"):
                log.warning("Couldn't load the downloaded local model")
                emit("load.failed", result.get("message") or "Load failed")
                _FAILED[key] = not_loaded
                return
            emit("load.completed")
            notify_cached_model_loaded(server_id, model.model)
        step = "catalog refresh"
        await discover_and_notify(server_id)
    except Exception as exc:
        log.warning("Not loading the downloaded local model after %s failed", step, exc_info=True)
        if step != "catalog refresh":
            emit("load.failed", f"{step} failed: {type(exc).__name__}")
            _FAILED[key] = not_loaded
