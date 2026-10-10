from __future__ import annotations

import asyncio

import httpx

from llm.providers import omlx
from omlx_provider_fixtures import CFG_PUBLIC, CFG_WITH_KEY, _patch_httpx, _resp, _Router


def test_url_builders_split_public_and_admin():
    assert omlx._api_base(CFG_PUBLIC) == "http://127.0.0.1:8000/v1"
    assert omlx._admin_base(CFG_PUBLIC) == "http://127.0.0.1:8000/admin/api"
    assert omlx._health_url(CFG_PUBLIC) == "http://127.0.0.1:8000/health"


def test_url_builders_return_none_for_missing_host_or_port():
    assert omlx._api_base({"host": "x"}) is None
    assert omlx._admin_base({"port": 1}) is None
    assert omlx._health_url({}) is None


def test_check_server_returns_running_when_health_200(monkeypatch):
    router = _Router({("GET", "/health"): _resp(200, {"status": "ok"})})
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.check_server(CFG_PUBLIC))
    assert out["running"] is True
    assert out["api_base"] == "http://127.0.0.1:8000/v1"
    assert out["server_id"] == "test-server-01"


def test_check_server_returns_not_running_on_500(monkeypatch):
    router = _Router({("GET", "/health"): _resp(500)})
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.check_server(CFG_PUBLIC))
    assert out["running"] is False


def test_check_server_handles_missing_host():
    out = asyncio.run(omlx.check_server({"server_id": "x"}))
    assert out["running"] is False



def test_list_loaded_uses_v1_models_status(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-XYZ-MLX-8bit",
                "loaded": True,
                "estimated_size": 36_449_409_793,
                "engine_type": "vlm",
                "pinned": False,
                "thinking_default": True,
                "preserve_thinking_default": True,
                "max_context_window": 256000,
                "last_access": 1778260000.0,
            },
            {
                "id": "Other-Model-31B-8bit",
                "loaded": False,        # filtered out
                "estimated_size": 31_000_000_000,
            },
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.list_loaded(CFG_PUBLIC))
    assert [m["identifier"] for m in out] == ["Model-XYZ-MLX-8bit"]
    m = out[0]
    assert m["status"] == "loaded"
    assert m["server_id"] == "test-server-01"
    assert m["size_bytes"] == 36_449_409_793
    assert m["context_length"] == 256000
    assert m["engine_type"] == "vlm"
    assert m["pinned"] is False
    assert m["thinking_default"] is True
    assert m["preserve_thinking_default"] is True
    assert m["last_access"] == 1778260000.0


def test_list_loaded_works_without_api_key(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "x", "loaded": True, "estimated_size": 1_000_000_000},
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.list_loaded(CFG_PUBLIC))  # CFG_PUBLIC has no api_key
    assert len(out) == 1
    assert out[0]["identifier"] == "x"


def test_list_loaded_derives_context_length_from_status_metadata(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "hf-qwen",
                "loaded": True,
                "estimated_size": 1_000_000_000,
                "config": {"max_position_embeddings": 262144},
            },
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.list_loaded(CFG_PUBLIC))
    assert out[0]["context_length"] == 262144


def test_list_loaded_returns_empty_on_status_endpoint_failure(monkeypatch):
    router = _Router({("GET", "/v1/models/status"): _resp(500)})
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.list_loaded(CFG_PUBLIC))
    assert out == []


def test_list_loaded_filters_loaded_false_entries(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "a", "loaded": True, "estimated_size": 1_000},
            {"id": "b", "loaded": False, "estimated_size": 2_000},
            {"id": "c", "loaded": True, "estimated_size": 3_000},
            {"id": "d", "loaded": None, "estimated_size": 4_000},  # absent → not loaded
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.list_loaded(CFG_PUBLIC))
    assert [m["identifier"] for m in out] == ["a", "c"]



def test_list_disk_models_returns_unloaded_from_status(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "in-ram", "loaded": True, "estimated_size": 36_000_000_000},
            {
                "id": "on-disk-1",
                "loaded": False,
                "estimated_size": 88_000_000_000,
                "model_max_length": 131072,
            },
            {
                "id": "on-disk-2",
                "loaded": False,
                "estimated_size": 4_500_000_000,
                "generation_config": {"max_position_embeddings": 65536},
            },
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.list_disk_models(CFG_PUBLIC))
    assert [m["identifier"] for m in out] == ["on-disk-1", "on-disk-2"]
    assert [m["size_bytes"] for m in out] == [88_000_000_000, 4_500_000_000]
    assert [m["context_length"] for m in out] == [131072, 65536]
    assert all(m["server_id"] == "test-server-01" for m in out)


def test_get_model_runtime_info_includes_context_length(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "hf-qwen",
                "loaded": True,
                "engine_type": "llm",
                "config": {"max_position_embeddings": 262144},
            },
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.get_model_runtime_info(CFG_PUBLIC, "hf-qwen"))
    assert out["ok"] is True
    assert out["context_length"] == 262144



def test_load_model_without_an_admin_key_makes_no_call_and_says_why(monkeypatch):
    router = _Router({
        ("POST", "/admin/api/models/qwen3-7b/load"): _resp(200, {"status": "loaded"}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.load_model(CFG_PUBLIC, "qwen3-7b"))
    assert out == {"ok": False, "message": "oMLX needs its admin key"}
    assert router.calls == []


def test_load_model_signs_in_then_posts_to_admin(monkeypatch):
    from llm.providers import omlx_endpoints

    omlx_endpoints._sessions.clear()
    router = _Router({
        ("POST", "/admin/api/models/qwen3-7b/load"): _resp(200, {"status": "loaded"}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.load_model(CFG_WITH_KEY, "qwen3-7b"))
    assert out["ok"] is True
    assert out["status"] == "loaded"
    assert [url.rsplit("/admin/api", 1)[1] for _, url, _ in router.calls] == [
        "/login", "/models/qwen3-7b/load",
    ]


def test_unload_model_prefers_public_path(monkeypatch):
    router = _Router({
        ("POST", "/v1/models/qwen3-7b/unload"): _resp(200, {"status": "unloaded"}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.unload_model(CFG_PUBLIC, "qwen3-7b"))
    assert out["ok"] is True
    assert all("/admin/" not in url for _, url, _ in router.calls)


def test_unload_model_falls_back_to_admin_on_401(monkeypatch):
    router = _Router({
        ("POST", "/v1/models/qwen3-7b/unload"): _resp(401),
        ("POST", "/admin/api/models/qwen3-7b/unload"): _resp(200, {"status": "unloaded"}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.unload_model(CFG_WITH_KEY, "qwen3-7b"))
    assert out["ok"] is True



def test_warm_cache_posts_with_explicit_model_id(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "DS-Model-flash-mxfp8", "loaded": True},
            {"id": "Model-XYZ-MLX-8bit", "loaded": True},
        ]}),
        ("POST", "/v1/chat/completions"): _resp(200, {
            "choices": [{"message": {"role": "assistant", "content": "."}}],
            "usage": {
                "prompt_tokens": 1543,
                "time_to_first_token": 0.42,
                "prompt_tokens_details": {"cached_tokens": 1500},
            },
        }),
    })
    _patch_httpx(monkeypatch, router)
    messages = [{"role": "system", "content": "system+project prompt"}]
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    out = asyncio.run(omlx.warm_cache(
        CFG_PUBLIC,
        model_id="Model-XYZ-MLX-8bit",
        messages=messages,
        tools=tools,
    ))
    assert out["ok"] is True
    assert out["model"] == "Model-XYZ-MLX-8bit"
    assert out["ttft_seconds"] == 0.42
    assert out["cached_tokens"] == 1500
    chat_calls = [c for c in router.calls if "/v1/chat/completions" in c[1]]
    assert len(chat_calls) == 1
    body = chat_calls[0][2]
    assert body["max_tokens"] == 1
    assert body["stream"] is False
    assert body["model"] == "Model-XYZ-MLX-8bit"
    assert body["messages"] == messages
    assert body["tools"] == tools


def test_edited_resend_keeps_serialized_omlx_request_prefix_and_tools(monkeypatch):
    """The direct oMLX request bytes may differ only at the edited user input."""
    captured: list[tuple[str, bytes]] = []

    class _CapturingRouter(_Router):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            captured.append((str(request.url), request.content))
            return super().__call__(request)

    router = _CapturingRouter({
        ("GET", "/v1/models/status"): _resp(200, {
            "models": [{"id": "Model-XYZ-MLX-8bit", "loaded": True}],
        }),
        ("POST", "/v1/chat/completions"): _resp(200, {
            "choices": [{"message": {"role": "assistant", "content": "."}}],
            "usage": {},
        }),
    })
    _patch_httpx(monkeypatch, router)
    canonical_tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    shared_prefix = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "unchanged history"},
        {"role": "assistant", "content": "unchanged response"},
    ]
    original_input = "original-final-input"
    edited_input = "edited-final-input"

    for user_input in (original_input, edited_input):
        result = asyncio.run(omlx.warm_cache(
            CFG_PUBLIC,
            model_id="Model-XYZ-MLX-8bit",
            messages=[*shared_prefix, {"role": "user", "content": user_input}],
            tools=canonical_tools,
        ))
        assert result["ok"] is True

    request_bodies = [
        body
        for url, body in captured
        if url.endswith("/v1/chat/completions")
    ]
    assert len(request_bodies) == 2
    original_body, edited_body = request_bodies
    assert original_body.replace(
        original_input.encode(),
        edited_input.encode(),
    ) == edited_body


def test_warm_cache_rejects_unloaded_target_without_http(monkeypatch):
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "Model-XYZ-MLX-8bit", "loaded": True},
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.warm_cache(
        CFG_PUBLIC,
        model_id="DS-Model-flash-mxfp8",  # not loaded
        messages=[{"role": "system", "content": "x"}],
    ))
    assert out["ok"] is False
    assert "model not loaded" in out["message"]
    assert "DS-Model-flash-mxfp8" in out["message"]
    chat_calls = [c for c in router.calls if "/v1/chat/completions" in c[1]]
    assert chat_calls == []


def test_warm_cache_rejects_empty_model_id():
    out = asyncio.run(omlx.warm_cache(CFG_PUBLIC, model_id="", messages=[]))
    assert out["ok"] is False
    assert "explicit model_id" in out["message"]



def test_probe_cache_returns_block_level_counts(monkeypatch):
    router = _Router({
        ("POST", "/admin/api/cache/probe"): _resp(200, {
            "model_id": "Model-XYZ-MLX-8bit",
            "model_loaded": True,
            "total_tokens": 8192,
            "block_size": 2048,
            "total_blocks": 4,
            "blocks_ssd_hot": 1,
            "blocks_ssd_disk": 1,
            "blocks_cold": 2,
            "ssd_hit_tokens": 4096,
            "cold_tokens": 4096,
        }),
    })
    _patch_httpx(monkeypatch, router)
    out = asyncio.run(omlx.probe_cache(
        CFG_WITH_KEY,
        model_id="Model-XYZ-MLX-8bit",
        messages=[{"role": "system", "content": "x"}],
    ))
    assert out["ok"] is True
    assert out["total_blocks"] == 4
    assert out["blocks_cold"] == 2
    assert out["block_size"] == 2048


def test_probe_cache_rejects_empty_model_id():
    out = asyncio.run(omlx.probe_cache(CFG_WITH_KEY, model_id="", messages=[]))
    assert out["ok"] is False
    assert "explicit model_id" in out["message"]
