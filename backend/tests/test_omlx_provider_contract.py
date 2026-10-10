from __future__ import annotations

import asyncio

import pytest

from llm.providers import omlx
from omlx_provider_fixtures import CFG_PUBLIC, CFG_WITH_KEY, _patch_httpx, _resp, _Router


def test_for_provider_returns_module_by_key():
    from llm.providers import for_provider, omlx as omlx_mod
    assert for_provider("omlx") is omlx_mod


def test_for_provider_raises_on_unknown_key():
    from llm.providers import for_provider
    with pytest.raises(ValueError, match="No provider module"):
        for_provider("bogus")


def test_omlx_implements_full_network_server_contract():
    from llm.providers import omlx as omlx_mod
    contract = {
        "check_server", "list_loaded", "list_disk_models",
        "load_model", "unload_model", "warm_cache", "get_cache_stats",
        "start_server", "stop_server", "_check_single",
        "get_settings", "update_settings", "shutdown", "get_model_config",
    }
    for fn in contract:
        assert hasattr(omlx_mod, fn), f"omlx.{fn} missing"


def test_omlx_lifecycle_start_requires_server_id():
    out_start = asyncio.run(omlx.start_server({}))
    out_stop = asyncio.run(omlx.stop_server({}))
    assert out_start["ok"] is False
    assert "server_id" in out_start["message"].lower()
    assert out_stop["ok"] is False
    assert "server_id" in out_stop["message"].lower()


def test_omlx_shutdown_stub_returns_unsupported(monkeypatch):
    out_shutdown = asyncio.run(omlx.shutdown({}))
    assert out_shutdown["ok"] is False
    assert "shutdown" in out_shutdown["message"].lower()


def test_omlx_check_single_delegates_to_check_server(monkeypatch):
    router = _Router({("GET", "/health"): _resp(200, {"status": "ok"})})
    _patch_httpx(monkeypatch, router)
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(
        network_discovery, "get_server",
        lambda sid: {"id": sid, "instance_name": "Studio", "port": 8000},
    )
    monkeypatch.setattr(
        network_endpoints, "get_active_endpoint",
        lambda sid: {"host": "127.0.0.1"},
    )
    out = asyncio.run(omlx._check_single({"id": "ns-1"}))
    assert out["running"] is True
    assert out["server_id"] == "ns-1"


def test_omlx_get_settings_with_key_pulls_global_settings(monkeypatch):
    router = _Router({
        ("GET", "/admin/api/global-settings"): _resp(200, {
            "max_process_memory": "auto",
            "paged_ssd_cache_dir": "/Users/x/.omlx/cache",
        }),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.get_settings(CFG_WITH_KEY))
    assert out["ok"] is True
    assert out["readonly"] is True
    assert "max_process_memory" in out["settings"]


def test_omlx_get_settings_without_key_returns_empty_readonly(monkeypatch):
    router = _Router({})
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.get_settings(CFG_PUBLIC))
    assert out == {"ok": True, "settings": {}, "readonly": True}
    assert router.calls == []


def test_omlx_update_settings_returns_unsupported(monkeypatch):
    out = asyncio.run(omlx.update_settings(CFG_WITH_KEY, {"foo": "bar"}))
    assert out["ok"] is False
    assert "CLI flags" in out["message"]


def test_omlx_update_model_settings_puts_to_admin_with_bearer(monkeypatch):
    router = _Router({
        ("PUT", "/admin/api/models/Model-XYZ-MLX-8bit/settings"): _resp(
            200, {"applied": True},
        ),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.update_model_settings(
        CFG_WITH_KEY, "Model-XYZ-MLX-8bit",
        {"pinned": True, "idle_timeout_seconds": 600},
    ))
    assert out["ok"] is True
    assert out["applied"] is True
    assert len(router.calls) == 1
    method, url, body = router.calls[0]
    assert method == "PUT"
    assert "/admin/api/models/Model-XYZ-MLX-8bit/settings" in url
    assert body == {"is_pinned": True, "ttl_seconds": 600}


def test_omlx_update_model_settings_without_an_admin_key_makes_no_call(monkeypatch):
    router = _Router({
        ("PUT", "/admin/api/models/x/settings"): _resp(200, {"applied": True}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.update_model_settings(CFG_PUBLIC, "x", {"pinned": True}))
    assert out["ok"] is False
    assert out["message"] == "oMLX needs its admin key"
    assert router.calls == []


def test_omlx_get_model_config_pulls_generation_config(monkeypatch):
    router = _Router({
        ("GET", "/admin/api/models/qwen3-7b/generation_config"): _resp(200, {
            "temperature": 0.7,
            "top_p": 0.9,
        }),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.get_model_config(CFG_WITH_KEY, "qwen3-7b"))
    assert out["ok"] is True
    assert out["config"]["temperature"] == 0.7




@pytest.mark.parametrize("effort,expected", [
    ("minimal", "minimal"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh", "xhigh"),
    ("max", "max"),
    (" HIGH ", "high"),    # normalised to the template's own vocabulary
    ("auto", None),        # defer to whatever the server is configured to use
    ("none", None),        # expressed by enable_thinking=False instead
    ("", None),
    (None, None),
])
def test_wire_reasoning_effort(effort, expected):
    """The level goes on the wire as reasoning_effort, the field templates read."""
    assert omlx.wire_reasoning_effort(effort) == expected


def test_unknown_vocabulary_passes_the_level_through():
    """With no reported vocabulary the server is the only validator.

    Dropping the value here would silently downgrade a model that does support
    the level, so it goes through and the server's own fallback decides.
    """
    assert omlx.wire_reasoning_effort("garbage") == "garbage"
    assert omlx.wire_reasoning_effort("garbage", None) == "garbage"


_LEVELS = {"type": "effort_levels", "values": ["xhigh", "medium", "low"], "default": "xhigh"}


def test_level_in_the_reported_vocabulary_is_forwarded():
    assert omlx.wire_reasoning_effort("medium", _LEVELS) == "medium"
    assert omlx.wire_reasoning_effort(" XHIGH ", _LEVELS) == "xhigh"


def test_level_outside_the_reported_vocabulary_is_refused_not_remapped():
    """oMLX would quietly substitute the model's default; that must surface."""
    import pytest
    from llm.providers.omlx_thinking import UnsupportedReasoningEffortError

    with pytest.raises(UnsupportedReasoningEffortError) as excinfo:
        omlx.wire_reasoning_effort("high", _LEVELS)
    assert excinfo.value.effort == "high"
    assert excinfo.value.values == ["xhigh", "medium", "low"]
    assert "xhigh, medium, low" in str(excinfo.value)


def test_off_and_auto_bypass_vocabulary_checks():
    assert omlx.wire_reasoning_effort("none", _LEVELS) is None
    assert omlx.wire_reasoning_effort("auto", _LEVELS) is None


def test_toggle_only_model_gets_no_level_on_the_wire():
    """On/Off models have no levels to name; ON means the server's own setting."""
    assert omlx.wire_reasoning_effort("medium", {"type": "boolean", "default": True}) is None


def test_toggle_only_model_on_asserts_thinking_explicitly():
    """ON must not merely defer: a server configured for off would show ON and not think."""
    toggle = {"type": "boolean", "default": True}
    assert omlx.make_inference_extra_body("medium", reasoning_config=toggle) == {
        "chat_template_kwargs": {"enable_thinking": True},
    }
    assert omlx.make_inference_extra_body("none", reasoning_config=toggle) == {
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert omlx.make_inference_extra_body("auto", reasoning_config=toggle) == {}
    # A level picker model does not get the toggle asserted; the level speaks.
    assert omlx.make_inference_extra_body("low", reasoning_config=_LEVELS) == {}


def test_prompt_shape_refuses_a_level_the_model_does_not_accept():
    import pytest
    from llm.providers.omlx_thinking import UnsupportedReasoningEffortError

    with pytest.raises(UnsupportedReasoningEffortError):
        omlx.make_prompt_extra_body([], "high", reasoning_config=_LEVELS)
    assert omlx.make_prompt_extra_body([], "low", reasoning_config=_LEVELS) == {
        "reasoning_effort": "low",
    }


def test_no_effort_level_puts_a_thinking_budget_on_the_wire():
    """A token ceiling is not a depth control; oMLX owns its per-model budget."""
    for effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
        assert "thinking_budget" not in omlx.make_inference_extra_body(effort)


def test_make_inference_extra_body_returns_empty_for_auto():
    assert omlx.make_inference_extra_body(None) == {}
    assert omlx.make_inference_extra_body("auto") == {}
    assert omlx.make_inference_extra_body("") == {}


def test_make_inference_extra_body_is_empty_for_levels():
    """Levels travel as reasoning_effort, so extra_body carries nothing."""
    assert omlx.make_inference_extra_body("low") == {}
    assert omlx.make_inference_extra_body("high") == {}
    assert omlx.make_inference_extra_body("max") == {}


def test_make_inference_extra_body_none_disables_thinking_template_kwarg():
    assert omlx.make_inference_extra_body("none") == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_make_inference_extra_body_normalises_case_and_whitespace():
    assert omlx.make_inference_extra_body(" HIGH ") == {}
    assert omlx.make_inference_extra_body("None") == {
        "chat_template_kwargs": {"enable_thinking": False},
    }
