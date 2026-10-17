"""The flights that use the person's own oMLX: adopt its models, or download the starter into it.

Neither writes any setting to that oMLX. Under "Let CORSIA manage it"
(``own_omlx_management``) adoption also reads its admin key and downloads the
recommender's pick into it unless that model is already there; its settings
change only once the person approves the shown values (``own_omlx_apply``).
The flight state and the oMLX calls live in ``local_setup``; they are read
through the module at call time so one set of names is patched and observed.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from llm import local_model, local_profiles, own_omlx_management
from llm import local_setup as ls
from llm.local_model import LocalModel, omlx_has_recommender
from llm.local_setup_ownership import own_omlx_admin_key, own_omlx_port

# The data-defined starter for an oMLX CORSIA does not manage, unchanged until #817
# decides that journey; CORSIA's own oMLX picks through its recommender instead.
OWN_OMLX_STARTER = local_profiles.OWN_OMLX_STARTER


async def _managed_key(port: int) -> str | None:
    """Under management only: their oMLX's admin key to register, or None to keep the stored one."""
    key = await asyncio.to_thread(own_omlx_admin_key)
    if key is None or await ls.registered_local_key(ls.LOCAL_HOST, port) is not None:
        return None
    await asyncio.to_thread(own_omlx_management.update, key_registered=True)
    return key


async def _managed_download(server_id: str, report: Callable[[str], None], listed: set[str]) -> bool:
    """Download the recommender's pick into their oMLX unless it is there; whether CORSIA set one up.

    An oMLX too old to recommend is recorded so its row says so. When the recommender
    answers none, the starter model is downloaded the same way and recorded as the
    fallback so setup says so; a computer under 24 GiB gets none. When it cannot
    answer, ``PickUnavailable`` propagates: setup ends in the failed check, with
    the cause as its message, as it does on a new oMLX, and installs nothing. A model the person already had stays theirs, so only the two
    oMLX-wide settings are offered.
    """
    version = await asyncio.to_thread(ls.omlx_version)
    if not omlx_has_recommender(version):
        await asyncio.to_thread(own_omlx_management.update, cannot_check=version or "unknown")
        return False
    await asyncio.to_thread(own_omlx_management.update, cannot_check=None)
    report("Finding the model that fits this computer")
    pick = await local_model.ask_recommender(server_id)
    fallback = pick is None
    if fallback:
        if not await asyncio.to_thread(ls.local_memory_ok):
            return False
        pick = OWN_OMLX_STARTER
    record = await asyncio.to_thread(own_omlx_management.read)
    ours = record is not None and record.model is not None and record.model.model == pick.model
    if pick.model in listed and not ours:
        return False
    await asyncio.to_thread(own_omlx_management.update, model=pick, fallback=fallback)
    if pick.model not in listed:
        ls._record_model(pick)
        await ls._download(server_id, report, pick)
    return True


async def adopt_existing_omlx(report: Callable[[str], None], events: list[str]) -> dict:
    """Register the person's own oMLX and its models; write no setting to it."""
    from infra.db_async import run_in_db
    from llm.catalog import list_models
    from llm.catalog_network_models import discover_network_models

    def declined(reason: str, message: str) -> dict:
        ls._emit("skipped", reason=reason)
        return {"ok": True, "skipped": reason, "message": message, "progress": events}

    async def listed() -> list[dict]:
        refreshed = await discover_network_models(server_id)
        if not refreshed.get("ok"):
            raise RuntimeError(refreshed.get("error") or "The local model catalog could not refresh.")
        return [row for row in await run_in_db(list_models, provider="omlx", include_unavailable=False)
                if row["litellm_model"].startswith(f"omlx@{server_id}/")]

    report("Checking your oMLX")
    port = await asyncio.to_thread(own_omlx_port)
    try:
        await ls._wait_for_health(port)
    except RuntimeError:
        return declined("existing_omlx_not_running",
                        "oMLX is installed but not running. Start it, then run `corsia local setup`.")
    managed = await asyncio.to_thread(own_omlx_management.active)
    server_id = await ls._register_loopback_server(port=port, api_key=await _managed_key(port) if managed else None)
    models = await listed()
    ids = {row["litellm_model"].removeprefix(f"omlx@{server_id}/") for row in models}
    if managed and await _managed_download(server_id, report, ids):
        # This discovery is the first to see the model's settings, which raises its decision again.
        report("Refreshing model catalog")
        models = await listed()
    if managed:
        from onboarding.omlx_settings_wake import rearm

        # Managing that starts in Settings comes after the first chat, so its decision is raised here.
        await rearm()
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
        return {"ok": False, "reason": "mac_cannot_run", "message": "This computer can't run a local model well."}
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
