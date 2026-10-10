"""The setup flight that installs and owns a new oMLX, then downloads the profile's model.

The flight state and the oMLX calls live in ``local_setup``; they are read
through the module at call time so one set of names is patched and observed.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Callable

from llm import local_setup as ls
from llm.local_profiles import LocalProfile
from llm.local_setup_gates import Eligibility


async def _setup_new_omlx(report: Callable[[str], None], eligibility: Eligibility, events: list[str],
                         profile: LocalProfile) -> dict:
    """Install and configure oMLX, download the chosen model and refresh the catalog."""
    report("Installing")
    needs_repair = not await asyncio.to_thread(ls._omlx_supports_loopback_auth)
    if needs_repair:
        version = await asyncio.to_thread(ls.omlx_version)
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
    await ls._download(server_id, report, profile)
    await ls._configure_model_settings(server_id, eligibility.context_window, profile)
    report("Refreshing model catalog")
    from llm.catalog_network_models import discover_network_models

    result = await discover_network_models(server_id)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "The local model catalog could not refresh.")
    ls._emit("completed", profile=profile.id)
    return {
        "ok": True, "server_id": server_id, "model": profile.model,
        "download_size": ls.format_size(profile.download_size_bytes),
        "eligibility": eligibility.__dict__, "progress": events,
    }
