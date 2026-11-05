"""Settings distinguishes a supervised restart from a server that is simply offline."""

import asyncio

import pytest

from api.network_servers import list_servers
from llm.network_discovery import upsert_server
from llm.providers import omlx_restart, omlx_supervisor


async def test_settings_projects_the_live_restart_until_it_ends(isolated_db, monkeypatch):
    sid = upsert_server("omlx", "projection-fixture", "CORSIA", "127.0.0.1", 9, "offline")
    life = omlx_restart.Life()
    monkeypatch.setattr(omlx_supervisor, "_life", {sid: life})
    waiting = asyncio.Event()
    task = omlx_restart.schedule(sid, life, waiting.wait())

    row = next(row for row in (await list_servers())["servers"] if row["id"] == sid)
    assert row["status"] == "offline"  # HTTP unavailability does not invent a lifecycle state
    assert row["restarting"] is True

    life.announce_stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = next(row for row in (await list_servers())["servers"] if row["id"] == sid)
    assert row["restarting"] is False
