"""The flights that use the person's own oMLX: adopt its models, or download a profile into it.

Neither writes any setting to that oMLX. The flight state and the oMLX calls live
in ``local_setup``; they are read through the module at call time so one set of
names is patched and observed.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from llm import local_setup as ls
from llm.local_profiles import DEFAULT_PROFILE, LocalProfile
from llm.local_setup_gates import Eligibility
from llm.local_setup_ownership import own_omlx_port


async def adopt_existing_omlx(report: Callable[[str], None], eligibility: Eligibility,
                               events: list[str]) -> dict:
    """Register the person's own oMLX and its models; write nothing to it."""
    from infra.db_async import run_in_db
    from llm.catalog import list_models
    from llm.catalog_network_models import discover_network_models

    def declined(reason: str, message: str) -> dict:
        ls._emit("skipped", reason=reason)
        return {"ok": True, "skipped": reason, "message": message,
                "eligibility": eligibility.__dict__, "progress": events}

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
            "eligibility": eligibility.__dict__, "progress": events}


async def download_into_own_omlx(report: Callable[[str], None], events: list[str], profile: LocalProfile) -> dict:
    """Download the profile's model into the person's own oMLX and refresh the catalog.

    Download only: no global or per-model setting is written to an oMLX CORSIA
    did not install.
    """
    from llm.catalog_network_models import discover_network_models

    report("Checking your oMLX")
    port = await asyncio.to_thread(own_omlx_port)
    await ls._wait_for_health(port)
    server_id = await ls._register_loopback_server(port=port)
    await ls._download(server_id, report, profile)
    report("Refreshing model catalog")
    refreshed = await discover_network_models(server_id)
    if not refreshed.get("ok"):
        raise RuntimeError(refreshed.get("error") or "The local model catalog could not refresh.")
    ls._emit("completed", existing_omlx=True, profile=profile.id)
    return {"ok": True, "server_id": server_id, "model": profile.model, "existing": True,
            "download_size": ls.format_size(profile.download_size_bytes), "progress": events}


async def begin_add_starter_model() -> dict:
    """Start downloading the default profile into the person's own oMLX, or say why not.

    Only an oMLX CORSIA did not install qualifies (the ownership marker decides):
    CORSIA's own is set up through ``begin_local_setup``. A request made
    while a flight runs reports that flight.
    """
    if ls._running():
        return ls._in_progress_result(ls._flight.eligibility)
    if ls.local_setup_operation.read() is not None:
        if await ls.resume_pending_setup():
            return ls._in_progress_result(ls._flight.eligibility)
        return {"ok": False, "reason": "recovery_failed", "message": "CORSIA could not resume the local model setup safely."}
    if not await asyncio.to_thread(ls.existing_install_is_foreign):
        return {"ok": False, "reason": "not_your_omlx",
                "message": "This oMLX was set up by CORSIA; use Set up for the local model."}
    eligibility = await asyncio.to_thread(ls.profile_eligibility, DEFAULT_PROFILE)
    if not eligibility.eligible:
        ls._emit("skipped", reason="ineligible", existing_omlx=True)
        return {"ok": False, "reason": "mac_cannot_run", "message": "This Mac can't run a local model well."}
    if not ls._claim_operation(DEFAULT_PROFILE, "starter"):
        if await ls.resume_pending_setup():
            return ls._in_progress_result(ls._flight.eligibility)
        return ls._in_progress_result(eligibility)
    ls._flight.result = None
    ls._flight.eligibility = eligibility
    ls._flight.stage = "Checking your oMLX"
    ls._flight.kind = "starter"
    ls._flight.task = asyncio.create_task(ls._run_flight(eligibility, into_own_omlx=True, profile=DEFAULT_PROFILE))
    ls._flight.task.add_done_callback(ls._observe_setup_task)
    ls._flight.task.add_done_callback(lambda _task: ls._notify_models_changed())
    ls._emit("started", existing_omlx=True)
    return ls._in_progress_result(eligibility)
