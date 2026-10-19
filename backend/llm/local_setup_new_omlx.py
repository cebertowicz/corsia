"""The setup flight on CORSIA's own oMLX: install and start it, take the pick, download it.

The flight state and the oMLX calls live in ``local_setup``; they are read
through the module at call time so one set of names is patched and observed.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from llm import local_setup as ls
from llm import local_setup_prepare as prepare
from llm.local_model import LocalModel, omlx_has_recommender, record_set_up
from llm.local_setup_managed import reserve_managed_omlx_port
from llm.local_setup_ownership import retire_legacy_owned_omlx_service


async def start_managed_omlx(report: Callable[[str], None]) -> tuple[str, int]:
    """Start CORSIA's supervised oMLX on its private state-root paths and port."""
    report("Installing")
    version = await asyncio.to_thread(ls.omlx_version)
    needs_repair = (not await asyncio.to_thread(ls._omlx_supports_loopback_auth)
                    or not omlx_has_recommender(version))
    if needs_repair:
        await asyncio.to_thread(ls._ensure_omlx, report, needs_repair=True, broken=version is None)
    else:
        await asyncio.to_thread(ls._ensure_omlx, report)
    if not await asyncio.to_thread(ls._omlx_supports_loopback_auth):
        raise RuntimeError(ls.UPDATE_OMLX_MESSAGE)
    # The old Homebrew service is stopped only after its marker and resolved
    # formula location prove CORSIA installed it. A user-owned oMLX is untouched.
    await asyncio.to_thread(retire_legacy_owned_omlx_service)
    paths, port = await asyncio.to_thread(reserve_managed_omlx_port)
    from llm.local_setup_runtime import register_managed_server
    from llm.providers.omlx_supervisor import start_server

    server_id, key = await register_managed_server(
        port=port, base_path=str(paths.base_path), model_dir=str(paths.model_dir),
    )
    report("Starting")
    started = await start_server({"server_id": server_id})
    if not started.get("ok"):
        raise RuntimeError(str(started.get("message") or "CORSIA couldn't start its local model server."))
    report("Configuring")
    await ls.secure_local_omlx(server_id, key)
    await ls._configure_global(server_id)
    return server_id, port


async def _setup_new_omlx(report: Callable[[str], None], events: list[str],
                         model: LocalModel | None = None) -> dict:
    """Set up CORSIA's own oMLX, download the picked model and refresh the catalog.

    ``model`` is a resumed flight's; otherwise the flight waits for a running
    prepare, takes its pick, or asks the recommender itself.
    """
    await prepare.join()
    server_id, port = await start_managed_omlx(report)
    if model is None:
        model = prepare.picked_model()
    if model is None:
        report("Finding the model that fits this computer")
        model = await prepare.ask(server_id, port)
    ls._record_model(model)
    await ls._download(server_id, report, model)
    await ls._configure_model_settings(server_id, model)
    report("Refreshing model catalog")
    from llm.catalog_network_models import discover_network_models

    result = await discover_network_models(server_id)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "The local model catalog could not refresh.")
    await asyncio.to_thread(record_set_up, model)
    ls._emit("completed", model=model.repository)
    return {
        "ok": True, "server_id": server_id, "model": model.model,
        "download_size": ls.format_size(model.download_size_bytes), "progress": events,
    }
