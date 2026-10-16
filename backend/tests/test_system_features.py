"""Settings feature responses always enumerate Focus alongside debug settings."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from api import system


def test_feature_snapshot_keeps_focus_default_on_when_only_debug_is_saved():
    assert system._features_snapshot({"debug_version_overlay": False}) == {
        "debug_version_overlay": False,
        "focus_mode": True,
        "cpu_profiler": False,
    }


def test_feature_snapshot_keeps_a_saved_focus_override():
    assert system._features_snapshot({"debug_version_overlay": True, "focus_mode": False}) == {
        "debug_version_overlay": True,
        "focus_mode": False,
        "cpu_profiler": False,
    }


# The build stamp names checkout commits, so it defaults off in a packaged app
# (no checkout to debug against) and on in a source checkout. A saved
# Settings > Debug value wins over the mode default in both.


def _set_platform_module_path(monkeypatch, tmp_path: Path, *, bundled: bool) -> None:
    """Make the real app_bundle_root() inspect a bundled or checkout path."""
    from infra import platform_paths

    module_path = (
        tmp_path / "CORSIA.app" / "Contents" / "Resources" / "backend" / "infra" / "x.py"
        if bundled
        else tmp_path / "checkout" / "backend" / "infra" / "x.py"
    )
    monkeypatch.setattr(platform_paths, "__file__", str(module_path))


@pytest.mark.parametrize("bundled", (True, False), ids=("packaged-app", "source-checkout"))
def test_debug_overlay_default_follows_the_real_app_bundle_path_check(
    monkeypatch, tmp_path, bundled,
):
    _set_platform_module_path(monkeypatch, tmp_path, bundled=bundled)
    assert system._features_snapshot({})["debug_version_overlay"] is not bundled


@pytest.mark.parametrize("bundled", (True, False), ids=("packaged-app", "source-checkout"))
@pytest.mark.parametrize("saved", (True, False), ids=("saved-on", "saved-off"))
def test_saved_debug_overlay_wins_over_the_mode_default(
    monkeypatch, tmp_path, bundled, saved,
):
    _set_platform_module_path(monkeypatch, tmp_path, bundled=bundled)
    assert system._features_snapshot({"debug_version_overlay": saved})[
        "debug_version_overlay"
    ] is saved


def _stub_features(monkeypatch, flags: dict | None = None, *, can_start: bool = True):
    """Route feature saves into a dict and drive a fake sampler that records applications."""
    store: dict = dict(flags or {})
    applied: list[bool] = []
    running = {"now": False}

    def apply(enabled: bool) -> bool:
        applied.append(enabled)
        running["now"] = enabled and can_start
        return running["now"]

    monkeypatch.setattr(system, "save_setting_merge", lambda _key, value: store.update(value))
    monkeypatch.setattr(system, "get_feature_flags", lambda: dict(store))
    monkeypatch.setattr(system, "apply_cpu_profiler", apply)
    monkeypatch.setattr(system, "cpu_sampler_running", lambda: running["now"])
    return store, applied


def test_cpu_profiler_defaults_off_in_the_snapshot(monkeypatch):
    monkeypatch.setattr(system, "cpu_sampler_running", lambda: False)
    assert system._features_snapshot({})["cpu_profiler"] is False


def test_features_patch_applies_the_saved_profiler_flag_on_and_off(monkeypatch):
    store, applied = _stub_features(monkeypatch)

    on = asyncio.run(system.update_settings({"features": {"cpu_profiler": True}}, user={"id": "u"}))
    off = asyncio.run(system.update_settings({"features": {"cpu_profiler": False}}, user={"id": "u"}))

    assert on["features"]["cpu_profiler"] is True
    assert off["features"]["cpu_profiler"] is False
    assert applied == [True, False]
    assert store == {"cpu_profiler": False}


def test_features_patch_reports_the_sampler_state_when_it_cannot_start(monkeypatch):
    store, applied = _stub_features(monkeypatch, can_start=False)

    updated = asyncio.run(system.update_settings({"features": {"cpu_profiler": True}}, user={"id": "u"}))

    assert store == {"cpu_profiler": True}
    assert applied == [True]
    assert updated["features"]["cpu_profiler"] is False


def test_features_patch_writes_only_booleans(monkeypatch):
    store, applied = _stub_features(monkeypatch)

    for features, message in (
        ({"cpu_profiler": "yes"}, "feature values must be booleans"),
        ({"focus_mode": 1}, "feature values must be booleans"),
        (True, "features must be an object"),
    ):
        with pytest.raises(HTTPException, match=message) as raised:
            asyncio.run(system.update_settings({"features": features}, user={"id": "u"}))
        assert raised.value.status_code == 400
    assert store == {} and applied == []


def test_debug_only_patch_returns_the_same_canonical_focus_default(monkeypatch):
    saved: list[tuple[str, dict]] = []
    monkeypatch.setattr(system, "save_setting_merge", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(system, "get_feature_flags", lambda: {"debug_version_overlay": False})

    updated = asyncio.run(system.update_settings(
        {"features": {"debug_version_overlay": False}}, user={"id": "user"},
    ))

    assert saved == [("features", {"debug_version_overlay": False})]
    assert updated["features"] == {
        "debug_version_overlay": False,
        "focus_mode": True,
        "cpu_profiler": False,
    }


def test_retired_core_model_patch_keys_fail_loud():
    for key, value in (("core_model", "mock/test-model"), ("fallback_model", None)):
        with pytest.raises(HTTPException, match="Core Agent model settings have been removed") as raised:
            asyncio.run(system.update_settings({key: value}, user={"id": "user"}))
        assert raised.value.status_code == 400


def test_live_bulletin_settings_require_human_and_safe_url():
    assert system._valid_live_bulletin_url("https://feed.example/bulletins")
    assert system._valid_live_bulletin_url("http://127.0.0.1:8000/bulletins")
    assert not system._valid_live_bulletin_url("http://feed.example/bulletins")
    assert not system._valid_live_bulletin_url("https://")
    assert not system._valid_live_bulletin_url(5)
    with pytest.raises(HTTPException, match="signed-in person") as raised:
        asyncio.run(system.update_settings(
            {"live_bulletins": {"enabled": True}}, user={"id": "agent", "token_kind": "agent"},
        ))
    assert raised.value.status_code == 403


def test_debug_only_patch_retains_saved_focus_off(monkeypatch):
    monkeypatch.setattr(system, "save_setting_merge", lambda _key, _value: None)
    monkeypatch.setattr(system, "get_feature_flags", lambda: {
        "debug_version_overlay": False, "focus_mode": False,
    })

    updated = asyncio.run(system.update_settings(
        {"features": {"debug_version_overlay": False}}, user={"id": "user"},
    ))

    assert updated["features"] == {
        "debug_version_overlay": False,
        "focus_mode": False,
        "cpu_profiler": False,
    }
