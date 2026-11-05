"""The held upgrade reports its cause without translating unrelated start errors."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from llm.providers import omlx_restart, omlx_supervisor


@pytest.mark.parametrize(("reason", "cause"), [
    ("busy", "busy"),
    ("not_idle", "couldn’t determine whether it was idle"),
    ("drain_timeout", "active requests did not finish in time"),
    ("loaded_unknown", "couldn’t determine which models were loaded"),
    ("stop_failed", "couldn’t stop"),
    ("start_failed", "couldn’t start"),
    ("reload_failed", "couldn’t restore"),
    ("unexpected", "Check the server in Settings"),
])
async def test_start_reports_held_upgrade_failure(monkeypatch, reason, cause):
    release = asyncio.Event()

    async def upgrade():
        await release.wait()
        return {"restarted": False, "reason": reason}

    operation = asyncio.create_task(upgrade())
    life = omlx_restart.Life(restart=operation, hold_requests=True)
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})
    start = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(omlx_supervisor, "_start_server", start)
    request = asyncio.create_task(omlx_supervisor.start_server({"server_id": "seat"}))
    await asyncio.sleep(0)
    assert not request.done()
    release.set()
    result = await request
    assert result["ok"] is False
    assert cause in result["message"]
    assert "try again" in result["message"].lower()
    start.assert_not_awaited()


async def test_start_does_not_translate_unrelated_runtime_error(monkeypatch):
    monkeypatch.setattr(omlx_supervisor, "_life", {})
    monkeypatch.setattr(omlx_supervisor, "_start_server", AsyncMock(side_effect=RuntimeError("internal detail")))
    with pytest.raises(RuntimeError, match="internal detail"):
        await omlx_supervisor.start_server({"server_id": "seat"})
