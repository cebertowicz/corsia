"""Clear the reply-length cap CORSIA's setup used to write into each local model's oMLX settings.

Until local models followed their own defaults, setup and a Settings download wrote ``max_tokens``
16,384 into every model they configured. This runs once per install, on the first startup where
CORSIA's own oMLX answers, and resets exactly that value so oMLX's own default applies; any other
value is the person's and is kept. Once it has run, a 16,384 set later is the person's too.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("llm.local_output_cap")

OLD_CAP = 16384
DONE_KEY = "local_models.old_output_cap_cleared"


async def clear_old_output_cap() -> bool:
    """Reset CORSIA's old 16,384 cap on each model of its own oMLX, once; True when it has run."""
    from infra.db_async import run_in_db
    from infra.settings import get_setting, set_setting
    from llm.local_sources import corsia_omlx_server

    if await run_in_db(get_setting, DONE_KEY) is not None:
        return True
    server = await run_in_db(corsia_omlx_server)
    if not server or server.get("status") != "online":
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
        if (model.get("settings") or {}).get("max_tokens") != OLD_CAP:
            continue
        result = await update_model_settings(cfg, model["id"], {"max_tokens": None})
        if not result.get("ok"):
            log.warning("Couldn't clear the old reply cap on %s: %s", model["id"], result.get("message"))
            return None
        cleared.append(model["id"])
    return cleared
