"""Report the outcome of an upgrade without certifying an incomplete restoration."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from llm.providers import omlx_restart as restart, omlx_upgrade as upgrade


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unknown", "The local model server is running, but CORSIA couldn’t verify which models to restore. Check the loaded models in Settings."),
        ("reload_failed", "The local model server is running, but CORSIA couldn’t restore its previous models. Check the loaded models in Settings."),
        ("exception", "CORSIA couldn’t get the local model server ready. Check the server and loaded models in Settings. Restart CORSIA if the server still needs updating."),
        ("timeout", "The local model server wasn’t ready in time. Check the server and loaded models in Settings. Restart CORSIA if the server still needs updating."),
    ],
)
async def test_post_start_failure_reports_outcome_and_holds_call(monkeypatch, case, message):
    life = restart.Life()
    entered, release = asyncio.Event(), asyncio.Event()
    events, calls = [], []
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _sid: ["hint"])
    monkeypatch.setattr("llm.server_observation.forget_loaded_on_exit", lambda _sid: ["exit-hint"])
    monkeypatch.setattr("infra.state.ws.broadcast_all", lambda _msg: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda **_kw: None)
    monkeypatch.setattr(restart, "emit", lambda _sid, action, **kw: events.append((action, kw)))

    async def state(_sid, *, reads):
        if case == "unknown":
            entered.set()
            await release.wait()
        return "idle"

    async def read(_cfg):
        calls.append("read")
        return [] if case == "reload_failed" and calls.count("read") == 2 else [{"identifier": "model"}]

    async def start(_cfg):
        calls.append("start")
        if case != "unknown":
            entered.set()
            await release.wait()
        return {"ok": True}

    async def reload(_sid, models):
        calls.append(("reload", list(models)))
        if case == "exception":
            raise ValueError("reload boom")
        if case == "timeout":
            raise TimeoutError("reload timeout")

    async def stop(_cfg):
        calls.append("stop")
        return {"ok": True}

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", state)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", read)
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(upgrade, "recorded_version", AsyncMock(return_value="old"))
    owner = upgrade.schedule_upgrade("seat", life, "old", "new", stop, start, lambda _sid: "", 5)
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)

        async def held():
            async with restart.request_slot({"seat": life}, "seat"):
                pytest.fail("failed restoration admitted a held call")

        waiter = asyncio.create_task(held())
        await asyncio.sleep(0)
        if case == "unknown":
            restart.on_exit("seat", life, 123, start, lambda _sid: "", ("crash", False))
        release.set()
        if case in ("exception", "timeout"):
            with pytest.raises(ValueError if case == "exception" else TimeoutError):
                await owner
        else:
            result = await owner
            expected = {"restarted": True, "reason": "loaded_unknown"} if case == "unknown" else {
                "restarted": True, "reason": "reload_failed", "missing": ["model"]
            }
            assert result == expected
        with pytest.raises(restart.UpgradeAdmissionError) as exc:
            await waiter
        assert str(exc.value) == message
        assert calls.count("start") == 1
        assert ("reload", ["exit-hint", "hint"] if case == "unknown" else ["model"]) in calls
        assert any(action == "restart.failed" for action, _ in events)
        assert not any(action == "restart.completed" for action, _ in events)
        assert upgrade.schedule_upgrade("seat", restart.Life(), "new", "new", stop, start, lambda _sid: "", 5) is None
        assert await upgrade._prepare("seat", restart.Life(), "new", "new", stop, 5, restart.Recovery([], 0)) == {
            "restarted": False, "reason": "already_current"
        }
    finally:
        release.set()
        for task in (waiter, owner):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (waiter, owner) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_unclassified_or_cancelled_owner_rejects_held_call(cancelled):
    release = asyncio.Event()

    async def operation():
        await release.wait()
        return {"restarted": True, "reason": "unclassified"}

    owner = asyncio.create_task(operation())
    life = restart.Life(restart=owner, hold_requests=True)

    async def held():
        async with restart.request_slot({"seat": life}, "seat"):
            pytest.fail("failed operation admitted a held call")

    waiter = asyncio.create_task(held())
    try:
        await asyncio.sleep(0)
        if cancelled:
            owner.cancel()
        else:
            release.set()
        with pytest.raises(restart.UpgradeAdmissionError) as exc:
            await waiter
        expected = (
            "The local model server update was cancelled. Check the server and loaded models in Settings. "
            "Restart CORSIA if the server still needs updating."
            if cancelled else
            "CORSIA couldn’t get the local model server ready. Check the server and loaded models in Settings. "
            "Restart CORSIA if the server still needs updating."
        )
        assert str(exc.value) == expected
        if cancelled:
            assert owner.cancelled()
        else:
            assert owner.result() == {"restarted": True, "reason": "unclassified"}
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(owner, waiter, return_exceptions=True)
