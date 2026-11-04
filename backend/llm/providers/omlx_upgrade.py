"""Bring CORSIA's running local model server onto the build this app ships, without a Quit.

An app update replaces the bundled oMLX, but a server already running keeps its old build: CORSIA adopts it at
startup and leaves it alone, so the new build's capacity waits until someone stops the server. The spawn record
carries the build it started (`omlx_process.write_pid`), so a difference from `OMLX_VERSION` is the one proof
available — oMLX reports no version over HTTP. A record with no build (written before this field existed) counts
as different, which is the population still sitting on old builds.

The restart waits for the seat to be provably idle (two `/api/status` reads, no active or waiting requests) and
then loads again exactly the models that were loaded, never another variant.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("llm.providers.omlx.upgrade")

QUIET_TIMEOUT_S = 3600.0
QUIET_INTERVAL_S = 60.0


async def upgrade_managed_omlx() -> dict[str, Any] | None:
    """Restart CORSIA's own online oMLX onto the bundled build once idle; None when there is nothing to do."""
    from infra.db_async import run_in_db
    from llm.local_sources import corsia_omlx_server
    from llm.omlx_release import OMLX_VERSION
    from llm.providers.omlx_supervisor import recorded_version

    try:
        server = await run_in_db(corsia_omlx_server)
        if not server or server.get("status") != "online":
            return None
        server_id = server["id"]
        recorded = await run_in_db(recorded_version, server_id)
        if recorded == OMLX_VERSION:
            return None
        return await _restart_when_idle(server_id, recorded, OMLX_VERSION)
    except Exception:  # never fail startup over an upgrade that can wait for the next one
        log.exception("The local model server's upgrade to %s did not run", OMLX_VERSION)
        return {"restarted": False, "reason": "error"}


async def _restart_when_idle(server_id: str, recorded: str | None, wanted: str) -> dict[str, Any]:
    """Stop and start the seat once it is idle, then reload the same models in order."""
    from infra.carry_in_capacity import omlx_state, wait_until_quiet
    from llm.local_keep_loaded import reload_after_restart
    from llm.providers import omlx_supervisor as supervisor
    from llm.providers.omlx_lifecycle import list_loaded

    log.info("The local model server is running %s and this app ships oMLX %s; waiting for it to be idle",
             recorded or "an unrecorded build", wanted)
    quiet = await wait_until_quiet(lambda: omlx_state(server_id, reads=2),
                                   timeout=QUIET_TIMEOUT_S, interval=QUIET_INTERVAL_S)
    if not quiet:
        log.info("The local model server stayed busy; it stays on %s for now", recorded or "its current build")
        return {"restarted": False, "reason": "not_idle"}

    loaded = sorted({row["identifier"] for row in await list_loaded({"server_id": server_id})})
    stopped = await supervisor.stop_server({"server_id": server_id})
    if not stopped.get("ok"):
        log.warning("Couldn't stop the local model server to move it to oMLX %s: %s", wanted, stopped.get("message"))
        return {"restarted": False, "reason": "stop_failed"}

    started = await supervisor.start_server({"server_id": server_id})
    if not started.get("ok"):
        log.error("The local model server did not come back on oMLX %s: %s", wanted, started.get("message"))
        return {"restarted": False, "reason": "start_failed"}

    await reload_after_restart(server_id, loaded)
    log.info("The local model server moved to oMLX %s with %d model(s) reloaded", wanted, len(loaded))
    return {"restarted": True, "loaded": loaded}
