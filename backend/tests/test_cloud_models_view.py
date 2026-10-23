"""Opening a cloud provider card answers from the last discovery and refreshes behind the read."""

from __future__ import annotations

import asyncio

import pytest

from llm import cloud_models_view as view

VALUE = "cloud-view-test-value-0123456789"


def _model(model_id: str, enabled: int = 0) -> dict:
    return {"id": model_id, "db_id": f"p-{model_id}", "display_name": model_id.upper(), "enabled": enabled}


@pytest.fixture
def discoveries(monkeypatch):
    """Stub discovery: records each call and remembers what the stub lists, as the real one does."""
    calls: list[dict] = []
    listed = {"models": [_model("a", 1), _model("b")]}

    async def discover(provider, key, *, notify=True):
        calls.append({"provider": provider, "notify": notify})
        models = [dict(m) for m in listed["models"]]
        view.remember(provider, key, models)
        return {"ok": True, "models": models}

    monkeypatch.setattr("llm.catalog.discover_cloud_models", discover)
    monkeypatch.setattr(view, "_enabled_by_id", lambda _provider: {"p-a": 1, "p-b": 1})
    changed: list[bool] = []
    monkeypatch.setattr(view, "notify_models_changed", lambda: changed.append(True))
    return calls, listed, changed


async def _settle() -> None:
    while view._refreshing:
        await asyncio.gather(*view._refreshing.values())


@pytest.mark.asyncio
async def test_the_second_read_answers_from_the_last_discovery_with_current_choices(discoveries):
    calls, _listed, _changed = discoveries

    first = await view.cloud_models_for_settings("anthropic", VALUE)
    second = await view.cloud_models_for_settings("anthropic", VALUE)

    assert len(calls) == 1
    assert [m["id"] for m in first["models"]] == ["a", "b"]
    # The user's on/off choices come from the database on every read, not from the remembered list.
    assert [(m["id"], m["enabled"]) for m in second["models"]] == [("a", 1), ("b", 1)]


@pytest.mark.asyncio
async def test_a_new_key_discovers_again(discoveries):
    calls, _listed, _changed = discoveries

    await view.cloud_models_for_settings("anthropic", VALUE)
    await view.cloud_models_for_settings("anthropic", VALUE + "-other")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_stale_list_is_refreshed_behind_the_read_and_clients_hear_only_of_a_change(discoveries, monkeypatch):
    calls, listed, changed = discoveries
    await view.cloud_models_for_settings("anthropic", VALUE)
    monkeypatch.setattr(view, "REFRESH_AFTER_SECONDS", -1)

    answer = await view.cloud_models_for_settings("anthropic", VALUE)
    await _settle()

    assert [m["id"] for m in answer["models"]] == ["a", "b"]
    assert calls[-1] == {"provider": "anthropic", "notify": False}
    assert changed == []

    listed["models"] = [_model("a", 1), _model("b"), _model("c")]
    await view.cloud_models_for_settings("anthropic", VALUE)
    await _settle()
    assert changed == [True]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_last_list_and_waits_a_full_interval(monkeypatch, discoveries):
    calls, _listed, _changed = discoveries
    await view.cloud_models_for_settings("anthropic", VALUE)

    async def failing(provider, key, *, notify=True):
        calls.append({"provider": provider, "notify": notify})
        return {"ok": False, "models": [], "error": "unreachable"}

    monkeypatch.setattr("llm.catalog.discover_cloud_models", failing)
    monkeypatch.setattr(view, "REFRESH_AFTER_SECONDS", -1)
    await view.cloud_models_for_settings("anthropic", VALUE)
    await _settle()
    monkeypatch.setattr(view, "REFRESH_AFTER_SECONDS", 600)
    answer = await view.cloud_models_for_settings("anthropic", VALUE)
    await _settle()

    assert [m["id"] for m in answer["models"]] == ["a", "b"]
    assert len(calls) == 2
