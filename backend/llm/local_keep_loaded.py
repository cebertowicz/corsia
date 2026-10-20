"""Keep a just-downloaded local model: pin it within the room for pins, then load it when it fits now.

oMLX's own ``is_pinned`` is the only pin record. A pinned model loads when oMLX
starts and is never unloaded to make room for another, so CORSIA keeps no pin
list. Setup's download and Add model's download both finish here.
"""

from __future__ import annotations

import logging

from infra.host_resources import GIB, check_can_load, pin_fits, pin_room_bytes, pins_full_line
from llm import local_downloads
from llm.local_model import LocalModel

log = logging.getLogger(__name__)

NOT_KEPT = "Downloaded, but couldn't keep it loaded. Turn it on to try again."
NOT_LOADED = "Not loaded: oMLX couldn't load it. Turn it on to try again."


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
    from llm.managed_omlx_binary import is_corsia_managed_server
    from llm.network_discovery import get_servers

    omlx = next((source for source in sources if source.get("source") == "omlx"), None)
    download = (omlx or {}).get("settings_download") or {}
    size = download.get("size_bytes")
    if not size or download.get("failed"):
        return
    servers = await run_in_db(get_servers, "omlx")
    server = next((s for s in servers if is_corsia_managed_server(s) and s.get("status") == "online"), None)
    rows = await _model_rows(server["id"]) if server else None
    if rows is not None:
        download["will_pin"] = pin_fits(int(size), _pinned_bytes(rows), pin_room_bytes())


async def keep_loaded(server_id: str, model: LocalModel) -> None:
    """Pin a completed download when it fits the room for pins, then load it when it fits now.

    Every outcome is a row state, never a failure: a pin that doesn't fit leaves
    the model downloaded and unpinned with the reason on its row, and a pin that
    couldn't be written says so. A completed download is never undone here.
    """
    from llm.network_discovery import discover_and_notify, notify_cached_model_loaded
    from llm.providers.omlx_lifecycle import load_model
    from llm.providers.omlx_settings import update_model_settings

    rows = await _model_rows(server_id)
    row = next((item for item in rows or [] if item.get("id") == model.model), None)
    size = row.get("estimated_size") if row else None
    if not isinstance(size, (int, float)) or size <= 0:
        log.warning("Not keeping the downloaded local model loaded: oMLX gave no usable size")
        local_downloads.set_detail(model.model, NOT_KEPT)
        return
    room = pin_room_bytes()
    pinned = _pinned_bytes(rows, skip=model.model)
    fits = pin_fits(int(size), pinned, room)
    if fits:
        pin = await update_model_settings({"server_id": server_id}, model.model, {"pinned": True})
        if not pin.get("ok"):
            log.warning("Couldn't pin the downloaded local model")
            local_downloads.set_detail(model.model, NOT_KEPT)
            return
    else:
        local_downloads.set_detail(model.model, pins_full_line(pinned, room))
    step = "memory check"
    try:
        needed_gb = float(size) / GIB
        decision = await check_can_load(needed_gb, "omlx")
        if not decision.get("ok"):
            # The row's "Not loaded" line comes from host_resources.annotate_keep_loaded.
            log.warning("Not loading the downloaded local model now: not enough free memory")
            return
        step = "model load"
        result = await load_model({"server_id": server_id}, model.model)
        if not result.get("ok"):
            log.warning("Couldn't load the downloaded local model")
            if fits:
                local_downloads.set_detail(model.model, NOT_LOADED)
            return
        notify_cached_model_loaded(server_id, model.model)
        step = "catalog refresh"
        await discover_and_notify(server_id)
    except Exception:
        log.warning("Not loading the downloaded local model after %s failed", step, exc_info=True)
        if step != "catalog refresh":
            local_downloads.set_detail(model.model, NOT_LOADED)
