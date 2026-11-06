"""Server lifecycle tags do not broaden ordinary catalog notifications."""

import asyncio

from infra.state import ws
from llm.providers import omlx_restart


async def test_restart_tags_both_transitions_without_changing_held_head_recovery(monkeypatch):
    broadcasts, recoveries = [], []
    monkeypatch.setattr(ws, "broadcast_all", broadcasts.append)
    monkeypatch.setattr(
        "infra.unavailable_model_recovery.schedule_unavailable_model_recovery",
        lambda: recoveries.append(True),
    )
    life = omlx_restart.Life()
    finish = asyncio.Event()
    task = omlx_restart.schedule("seat", life, finish.wait(), hold_requests=True)
    try:
        assert broadcasts == [{"type": "models_updated", "server_id": "seat"}]
        assert recoveries == []  # pending work stays held during restoration
        finish.set()
        await task
        await asyncio.sleep(0)
        assert life.restart is None
        assert broadcasts == [{"type": "models_updated", "server_id": "seat"}] * 2
        assert recoveries == [True]
        ws.notify_models_changed()
        assert broadcasts[-1] == {"type": "models_updated"}
        assert recoveries == [True, True]
    finally:
        finish.set()
        await task
