"""The held upgrade reports its cause without translating unrelated start errors."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from llm.providers import omlx_restart, omlx_supervisor


@pytest.mark.parametrize(("reason", "cause"), [
    ("busy", "Couldn’t upgrade the local model server because the server was busy. Wait for its activity to finish. Restart CORSIA to retry the upgrade."),
    ("not_idle", "Couldn’t upgrade the local model server because CORSIA couldn’t determine whether it was idle. Check the server in Settings. Restart CORSIA to retry the upgrade."),
    ("drain_timeout", "Couldn’t upgrade the local model server because active requests did not finish in time. Wait for them to finish. Restart CORSIA to retry the upgrade."),
    ("loaded_unknown", "Couldn’t upgrade the local model server because CORSIA couldn’t determine which models were loaded. Check the server in Settings. Restart CORSIA to retry the upgrade."),
    ("stop_failed", "Couldn’t upgrade the local model server because CORSIA couldn’t stop the server. Check the server in Settings. Restart CORSIA to retry the upgrade."),
    ("start_failed", "Couldn’t upgrade the local model server because CORSIA couldn’t start the server. Check the server in Settings. Restart CORSIA to retry the upgrade."),
    ("reload_failed", "Couldn’t upgrade the local model server because CORSIA couldn’t restore the loaded models. Check the server in Settings. Restart CORSIA to retry the upgrade."),
    ("unexpected", "CORSIA couldn’t get the local model server ready. Check the server and loaded models in Settings. Restart CORSIA if the server still needs updating."),
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
    assert result["message"] == cause
    start.assert_not_awaited()


async def test_cancelled_upgrade_reports_retry_instructions():
    operation = asyncio.create_task(asyncio.Event().wait())
    life = omlx_restart.Life(restart=operation, hold_requests=True)
    operation.cancel()
    with pytest.raises(omlx_restart.UpgradeAdmissionError) as error:
        async with omlx_restart.request_slot({"seat": life}, "seat"):
            pytest.fail("a call passed a cancelled upgrade")
    assert str(error.value) == (
        "The local model server update was cancelled. Check the server and loaded models in Settings. "
        "Restart CORSIA if the server still needs updating."
    )


async def test_start_does_not_translate_unrelated_runtime_error(monkeypatch):
    monkeypatch.setattr(omlx_supervisor, "_life", {})
    monkeypatch.setattr(omlx_supervisor, "_start_server", AsyncMock(side_effect=RuntimeError("internal detail")))
    with pytest.raises(RuntimeError, match="internal detail"):
        await omlx_supervisor.start_server({"server_id": "seat"})
