"""Clear the reply-length cap CORSIA's setup used to write into each local model's oMLX settings.

Until local models followed their own defaults, setup and a Settings download wrote the same block into every model
they configured: ``max_tokens`` 16,384 alongside CORSIA's own KV block (``turboquant_kv_enabled`` true and
``turboquant_kv_bits`` 8, from ``local_profiles.json``). That combination is CORSIA's signature — oMLX serves no
KV quantization unless it is asked — so it is what this cleanup claims, and it resets only the cap part of it,
leaving the KV block and everything else as it is. A model without that signature is untouched: its limit is the
person's, and the limit it keeps is logged so a readout is possible.

It runs once per install, on its first startup in this build. When CORSIA has configured nothing yet (a fresh
install, where startup runs before setup) there is no cap of CORSIA's to clear, so it records that it has run and
never touches anything afterwards.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("llm.local_output_cap")

OLD_CAP = 16384
DONE_KEY = "local_models.old_output_cap_cleared"


def _written_by_setup(settings: Any) -> bool:
    """True for the settings block setup wrote: the cap together with CORSIA's own KV block."""
    return isinstance(settings, dict) and settings.get("turboquant_kv_enabled") is True \
        and settings.get("turboquant_kv_bits") == 8


async def clear_old_output_cap() -> bool:
    """Run the one-time cleanup; True when this install has had it."""
    from infra.db_async import run_in_db
    from infra.settings import get_setting, set_setting
    from llm.local_sources import corsia_omlx_server

    if await run_in_db(get_setting, DONE_KEY) is not None:
        return True
    server = await run_in_db(corsia_omlx_server)
    if server is None:
        # Nothing has been configured here, so no cap of CORSIA's exists to clear; later ones are the person's.
        await run_in_db(set_setting, DONE_KEY, {"models": [], "reason": "no_managed_server"})
        return True
    if server.get("status") != "online":
        return False  # tried again at the next startup
    cleared = await _reset_capped_models({"server_id": server["id"]})
    if cleared is None:
        return False
    await run_in_db(set_setting, DONE_KEY, {"models": cleared})
    log.info("Cleared CORSIA's old %d-token reply cap on %d local model(s)", OLD_CAP, len(cleared))
    return True


async def _reset_capped_models(cfg: dict[str, Any]) -> list[str] | None:
    """The models whose cap was reset, or None when oMLX couldn't be read or a reset failed."""
    from llm.providers.omlx_endpoints import admin_request
    from llm.providers.omlx_settings import update_model_settings

    try:
        response = await admin_request(cfg, "GET", "/models")
        data = response.json() if response.status_code == 200 else None
    except Exception:
        log.warning("Couldn't read local model settings to clear the old reply cap", exc_info=True)
        return None
    if isinstance(data, dict):
        data = data.get("models")
    if not isinstance(data, list):
        return None
    cleared = []
    for model in data:
        settings = model.get("settings") or {}
        if settings.get("max_tokens") != OLD_CAP:
            continue
        model_id = model.get("id")
        if not _written_by_setup(settings):
            log.info("Kept the %d-token reply limit on %s: its settings aren't the block setup writes",
                     OLD_CAP, model_id)
            continue
        result = await update_model_settings(cfg, model_id, {"max_tokens": None})
        if not result.get("ok"):
            log.warning("Couldn't clear the old reply cap on %s: %s", model_id, result.get("message"))
            return None
        cleared.append(model_id)
    return cleared
