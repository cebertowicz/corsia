"""The setup flight on CORSIA's own oMLX: install and start it, take the pick, download it.

The flight state and the oMLX calls live in ``local_setup``; they are read
through the module at call time so one set of names is patched and observed.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Callable

from llm import local_setup as ls
from llm import local_setup_prepare as prepare
from llm.local_model import LocalModel, omlx_has_recommender, record_set_up


async def start_managed_omlx(report: Callable[[str], None]) -> tuple[str, int]:
    """Install, start and secure CORSIA's own oMLX; return its server id and port.

    Prepare and the flight share it. An oMLX older than the recommender is
    updated with the same repair as one older than loopback auth.
    """
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
    report("Starting")
    # `omlx start` runs `omlx serve` as a brew service on its default 127.0.0.1. It takes
    # no address (`serve --host` would write one into oMLX's settings), so
    # secure_local_omlx proves the bind is loopback before the skip flag is set.
    # It serves on the port in oMLX's settings file (a leftover one may set any
    # port), so the port is read from there, never assumed.
    await asyncio.to_thread(ls._run, ["omlx", "start"])
    port = await asyncio.to_thread(ls.own_omlx_port)
    await ls._wait_for_health(port)
    # A leftover settings file may already hold an admin key, and oMLX signs in
    # with no other, so that key wins over the registered one or a new one.
    key = (await asyncio.to_thread(ls.own_omlx_admin_key)
           or await ls.registered_local_key(ls.LOCAL_HOST, port) or secrets.token_urlsafe(32))
    server_id = await ls._register_loopback_server(port=port, api_key=key)
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
