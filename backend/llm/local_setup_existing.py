"""The flights that use the person's own oMLX: adopt its models, or download the starter into it.

Neither writes any setting to that oMLX. The flight state and the oMLX calls live
in ``local_setup``; they are read through the module at call time so one set of
names is patched and observed.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from llm import local_setup as ls
from llm.local_model import LocalModel
from llm.local_setup_ownership import own_omlx_port

# The starter download for an oMLX CORSIA does not manage, unchanged until #817
# decides that journey; CORSIA's own oMLX picks through its recommender instead.
OWN_OMLX_STARTER = LocalModel(
    repository="prism-ml/Ternary-Bonsai-2-27B-mlx-2bit",
    download_size_bytes=8_608_739_065,
    context_window=32_768,
)


async def adopt_existing_omlx(report: Callable[[str], None], events: list[str]) -> dict:
    """Register the person's own oMLX and its models; write nothing to it."""
    from infra.db_async import run_in_db
    from llm.catalog import list_models
    from llm.catalog_network_models import discover_network_models

    def declined(reason: str, message: str) -> dict:
        ls._emit("skipped", reason=reason)
        return {"ok": True, "skipped": reason, "message": message, "progress": events}

    report("Checking your oMLX")
    port = await asyncio.to_thread(own_omlx_port)
    try:
        await ls._wait_for_health(port)
    except RuntimeError:
        return declined("existing_omlx_not_running",
                        "oMLX is installed but not running. Start it, then run `corsia local setup`.")
    server_id = await ls._register_loopback_server(port=port)
    refreshed = await discover_network_models(server_id)
    if not refreshed.get("ok"):
        raise RuntimeError(refreshed.get("error") or "The local model catalog could not refresh.")
    models = [row for row in await run_in_db(list_models, provider="omlx", include_unavailable=False)
              if row["litellm_model"].startswith(f"omlx@{server_id}/")]
    if not models:
        return declined("existing_omlx_no_models",
                        "Your oMLX has no models yet. You can add the local model from the app.")
    ls._emit("completed", existing_omlx=True)
    return {"ok": True, "server_id": server_id, "existing": True, "models": len(models),
            "progress": events}


async def download_into_own_omlx(report: Callable[[str], None], events: list[str], model: LocalModel) -> dict:
    """Download ``model`` into the person's own oMLX and refresh the catalog.

    Download only: no global or per-model setting is written to an oMLX CORSIA
    did not install.
    """
    from llm.catalog_network_models import discover_network_models

    report("Checking your oMLX")
    port = await asyncio.to_thread(own_omlx_port)
    await ls._wait_for_health(port)
    server_id = await ls._register_loopback_server(port=port)
    await ls._download(server_id, report, model)
    report("Refreshing model catalog")
    refreshed = await discover_network_models(server_id)
    if not refreshed.get("ok"):
        raise RuntimeError(refreshed.get("error") or "The local model catalog could not refresh.")
    ls._emit("completed", existing_omlx=True, model=model.repository)
    return {"ok": True, "server_id": server_id, "model": model.model, "existing": True,
            "download_size": ls.format_size(model.download_size_bytes), "progress": events}


async def begin_add_starter_model() -> dict:
    """Start downloading the starter into the person's own oMLX, or say why not.

    Only an oMLX CORSIA did not install qualifies (the ownership marker decides):
    CORSIA's own is set up through ``begin_local_setup``. The same 24 GiB gate
    applies. A request made while a flight runs reports that flight.
    """
    if ls._running():
        return ls._in_progress_result()
    if ls.local_setup_operation.read() is not None:
        if await ls.resume_pending_setup():
            return ls._in_progress_result()
        return {"ok": False, "reason": "recovery_failed", "message": "CORSIA could not resume the local model setup safely."}
    if not await asyncio.to_thread(ls.existing_install_is_foreign):
        return {"ok": False, "reason": "not_your_omlx",
                "message": "This oMLX was set up by CORSIA; use Set up for the local model."}
    if not await asyncio.to_thread(ls.local_memory_ok):
        ls._emit("skipped", reason="ineligible", existing_omlx=True)
        return {"ok": False, "reason": "mac_cannot_run", "message": "This Mac can't run a local model well."}
    if not ls._claim_operation("starter", OWN_OMLX_STARTER):
        await ls.resume_pending_setup()
        return ls._in_progress_result()
    ls._flight.result = None
    ls._flight.stage = "Checking your oMLX"
    ls._flight.task = asyncio.create_task(ls._run_flight(model=OWN_OMLX_STARTER, into_own_omlx=True))
    ls._flight.task.add_done_callback(ls._observe_setup_task)
    ls._flight.task.add_done_callback(lambda _task: ls._notify_models_changed())
    ls._emit("started", existing_omlx=True)
    return ls._in_progress_result()
