"""oMLX server and model lifecycle operations."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from llm.model_metadata import extract_context_length
from llm.providers.omlx_public_auth import OmlxPublicAuthError, public_headers
from llm.providers.omlx_endpoints import (
    _TIMEOUT,
    OmlxAdminError,
    _api_base,
    admin_request,
    _health_url,
    _resolve_endpoint,
)

log = logging.getLogger("llm.providers.omlx")

_LOAD_TIMEOUT = 600.0
# A clean install can process tens of thousands of uncached prefix tokens.
# Keep connection failure quick while allowing that one bounded warm to finish.
_WARM_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


class OmlxLoadedStateError(RuntimeError):
    """The provider could not prove a model is already loaded."""


class OmlxModelNotLoadedError(OmlxLoadedStateError):
    """The provider authoritatively reported that a model is not loaded."""


async def require_loaded_model(
    api_base: str | None,
    model: str,
    *,
    server_id: str | None = None,
) -> None:
    """Fail closed unless ``model`` is loaded in the live oMLX status snapshot.

    oMLX chat completion implicitly loads a missing model and may evict another
    one to make room. Every CORSIA-owned inference call therefore checks the
    public status surface immediately before dispatch instead of relying on the
    swept catalog cache.
    """
    provider_model_id = str(model or "").removeprefix("openai/")
    base = str(api_base or "").rstrip("/")
    if not base or not provider_model_id:
        raise OmlxLoadedStateError("oMLX loaded-state check requires endpoint and model")

    try:
        headers = await public_headers(
            {"server_id": server_id}, url=f"{base}/models/status",
        ) if server_id else {}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(f"{base}/models/status", headers=headers)
    except (httpx.HTTPError, OmlxPublicAuthError) as exc:
        raise OmlxLoadedStateError(
            f"oMLX loaded-state check failed for {provider_model_id}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise OmlxLoadedStateError(
            "oMLX loaded-state check failed for "
            f"{provider_model_id}: HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise OmlxLoadedStateError(
            f"oMLX loaded-state check returned invalid JSON for {provider_model_id}"
        ) from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise OmlxLoadedStateError(
            f"oMLX loaded-state check returned an invalid payload for {provider_model_id}"
        )
    if any(
        isinstance(entry, dict)
        and entry.get("id") == provider_model_id
        and entry.get("loaded") is True
        for entry in models
    ):
        return
    raise OmlxModelNotLoadedError(f"model not loaded: {provider_model_id}")


async def check_server(cfg: dict) -> dict[str, Any]:
    """Ping the server's ``/health`` endpoint. Returns running + endpoint info."""
    health = _health_url(cfg)
    api_base = _api_base(cfg)
    if not health or not api_base:
        return {"running": False, "server_id": cfg.get("server_id"), "error": "Missing host/port"}
    _, port = _resolve_endpoint(cfg)
    try:
        headers = await public_headers(cfg, url=health)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(health, headers=headers)
            return {
                "running": resp.status_code == 200,
                "api_base": api_base,
                "port": port,
                "server_id": cfg.get("server_id"),
            }
    except Exception:
        return {"running": False, "server_id": cfg.get("server_id")}


async def _loaded_snapshot(cfg: dict) -> tuple[list[dict], bool]:
    """Read once; retain known loaded rows and say whether the snapshot is complete."""
    api_base = _api_base(cfg)
    if not api_base:
        return [], False
    try:
        headers = await public_headers(cfg, url=f"{api_base}/models/status")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{api_base}/models/status", headers=headers)
            if resp.status_code != 200:
                return [], False
            payload = resp.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return [], False
            complete = all(
                isinstance(m, dict) and bool(m.get("id")) and isinstance(m.get("loaded"), bool)
                for m in models
            )
            rows = [
                {
                    "identifier": m.get("id", ""),
                    "server_id": cfg.get("server_id"),
                    "server_name": cfg.get("instance_name"),
                    "status": "loaded",
                    "size_bytes": int(m.get("estimated_size") or 0),
                    "context_length": extract_context_length(m),
                    "engine_type": m.get("engine_type"),
                    "pinned": m.get("pinned"),
                    "thinking_default": m.get("thinking_default"),
                    "preserve_thinking_default": m.get("preserve_thinking_default"),
                    "last_access": m.get("last_access"),
                }
                for m in models if isinstance(m, dict) and m.get("id") and m.get("loaded") is True
            ]
            return rows, complete
    except (httpx.HTTPError, OmlxPublicAuthError, ValueError, TypeError) as e:
        log.warning("oMLX loaded status failed: %s", e)
        return [], False


async def read_loaded(cfg: dict) -> list[dict] | None:
    """Return loaded rows only when the live status is complete."""
    rows, complete = await _loaded_snapshot(cfg)
    return rows if complete else None


async def list_loaded(cfg: dict) -> list[dict]:
    """Return the known loaded rows, or an empty list when the read fails."""
    rows, _complete = await _loaded_snapshot(cfg)
    return rows


async def read_models_status(cfg: dict) -> list[dict] | None:
    """Read the complete model status once, or report an unavailable snapshot."""
    api_base = _api_base(cfg)
    if not api_base:
        log.warning("oMLX model status failed: missing endpoint")
        return None
    try:
        headers = await public_headers(cfg, url=f"{api_base}/models/status")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{api_base}/models/status", headers=headers)
        if resp.status_code != 200:
            log.warning("oMLX model status failed: HTTP %s", resp.status_code)
            return None
        payload = resp.json()
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            log.warning("oMLX model status failed: invalid model list")
            return None
        return [{"identifier": m.get("id", ""), "loaded": m.get("loaded") is True,
                 "size_bytes": int(m.get("estimated_size") or 0)}
                for m in models if isinstance(m, dict) and m.get("id")]
    except (httpx.HTTPError, OmlxPublicAuthError, ValueError, TypeError) as e:
        log.warning("oMLX model status failed: %s", e)
        return None


async def list_disk_models(cfg: dict) -> list[dict]:
    """Return discovered-but-not-loaded models from ``GET /v1/models/status``."""
    api_base = _api_base(cfg)
    if not api_base:
        return []
    try:
        headers = await public_headers(cfg, url=f"{api_base}/models/status")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{api_base}/models/status", headers=headers)
            if resp.status_code != 200:
                return []
            return [
                {
                    "identifier": m.get("id", ""),
                    "size_bytes": int(m.get("estimated_size") or 0),
                    "context_length": extract_context_length(m),
                    "server_id": cfg.get("server_id"),
                    "server_name": cfg.get("instance_name"),
                }
                for m in (resp.json().get("models") or [])
                if m.get("id") and m.get("loaded") is False
            ]
    except (httpx.HTTPError, ValueError) as e:
        from loguru import logger
        logger.warning(f"oMLX list_disk_models failed: {e}")
        return []


async def load_model(cfg: dict, model_key: str, **_opts) -> dict[str, Any]:
    """Load a model via the admin endpoint."""
    try:
        resp = await admin_request(
            cfg, "POST", f"/models/{model_key}/load", json={}, timeout=_LOAD_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            return {"ok": True, "message": f"Loaded {model_key}", **data}
        return {
            "ok": False,
            "status": resp.status_code,
            "message": _extract_omlx_error(resp),
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _extract_omlx_error(resp: httpx.Response) -> str:
    """Pull the human-readable detail out of an oMLX error response."""
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except ValueError:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


async def unload_model(cfg: dict, model_key: str) -> dict[str, Any]:
    """Unload a model, falling back to the admin endpoint when needed."""
    api_base = _api_base(cfg)
    if not api_base:
        return {"ok": False, "message": "Server has no reachable endpoint"}
    try:
        headers = await public_headers(cfg, url=f"{api_base}/models/{model_key}/unload")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{api_base}/models/{model_key}/unload", headers=headers,
            )
            if resp.status_code == 200:
                return {"ok": True, "message": f"Unloaded {model_key}"}
            if resp.status_code in (401, 403, 404):
                try:
                    ar = await admin_request(cfg, "POST", f"/models/{model_key}/unload", json={})
                except OmlxAdminError:
                    ar = None  # no key or no sign-in: report the public route's answer below
                if ar is not None:
                    if ar.status_code == 200:
                        return {"ok": True, "message": f"Unloaded {model_key}"}
                    return {"ok": False, "message": f"HTTP {ar.status_code}: {ar.text[:200]}"}
            return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


async def warm_cache(
    cfg: dict,
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
    thinking_budget: int | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Pre-warm KV cache for a loaded model via ``max_tokens=1``."""
    api_base = _api_base(cfg)
    if not api_base:
        return {"ok": False, "message": "Server has no reachable endpoint"}
    if not model_id:
        return {"ok": False, "message": "warm_cache requires explicit model_id"}

    loaded = await list_loaded(cfg)
    loaded_ids = {m.get("identifier", "") for m in loaded}
    if model_id not in loaded_ids:
        return {
            "ok": False,
            "message": f"model not loaded: {model_id}",
            "loaded_models": sorted(loaded_ids),
        }

    body: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "max_tokens": 1,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    if chat_template_kwargs:
        body["chat_template_kwargs"] = chat_template_kwargs
    if thinking_budget is not None:
        body["thinking_budget"] = thinking_budget
    # The chat template injects the effort level into the prompt, so warming
    # without it would build a different prefix than the turn sends.
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort

    try:
        headers = await public_headers(cfg, url=f"{api_base}/chat/completions")
        async with httpx.AsyncClient(timeout=_WARM_TIMEOUT) as client:
            resp = await client.post(
                f"{api_base}/chat/completions", headers=headers, json=body,
            )
            if resp.status_code == 200:
                usage = (resp.json() or {}).get("usage", {}) or {}
                return {
                    "ok": True,
                    "model": model_id,
                    "ttft_seconds": usage.get("time_to_first_token"),
                    "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                }
            return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        log.warning("warm_cache failed: %s", e)
        return {"ok": False, "message": str(e)}


async def probe_cache(
    cfg: dict,
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
    thinking_budget: int | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Probe oMLX block-level cache state for a prefix without running prefill."""
    if not model_id:
        return {"ok": False, "message": "probe_cache requires explicit model_id"}

    body: dict[str, Any] = {"model_id": model_id, "messages": messages}
    if tools:
        body["tools"] = tools
    if chat_template_kwargs:
        body["chat_template_kwargs"] = chat_template_kwargs
    if thinking_budget is not None:
        body["thinking_budget"] = thinking_budget
    # Same reason as warm_cache: a probe that omits the effort measures a
    # different prefix than the one the turn will actually send.
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort

    try:
        resp = await admin_request(cfg, "POST", "/cache/probe", json=body)
        if resp.status_code != 200:
            return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json() or {}
        return {
            "ok": True,
            "model_id": data.get("model_id"),
            "model_loaded": data.get("model_loaded", False),
            "total_tokens": data.get("total_tokens", 0),
            "block_size": data.get("block_size", 0),
            "total_blocks": data.get("total_blocks", 0),
            "blocks_ssd_hot": data.get("blocks_ssd_hot", 0),
            "blocks_ssd_disk": data.get("blocks_ssd_disk", 0),
            "blocks_cold": data.get("blocks_cold", 0),
            "ssd_hit_tokens": data.get("ssd_hit_tokens", 0),
            "cold_tokens": data.get("cold_tokens", 0),
        }
    except (httpx.HTTPError, ValueError, OmlxAdminError) as e:
        log.warning("probe_cache failed: %s", e)
        return {"ok": False, "message": str(e)}


async def get_cache_stats(cfg: dict) -> dict[str, Any]:
    """Fetch SSD + hot-cache stats via admin ``/stats``. Requires api_key."""
    try:
        resp = await admin_request(cfg, "GET", "/stats")
        if resp.status_code == 200:
            return {"ok": True, **(resp.json() or {})}
        return {"ok": False, "message": f"HTTP {resp.status_code}"}
    except Exception as e:
        log.warning("get_cache_stats failed: %s", e)
        return {"ok": False, "message": str(e)}


async def start_server(cfg: dict) -> dict[str, Any]:
    """Spawn an ``omlx serve`` subprocess for this network-servers row."""
    from llm.providers.omlx_supervisor import start_server as _start
    return await _start(cfg)


async def stop_server(cfg: dict) -> dict[str, Any]:
    """Stop the oMLX subprocess spawned for this row."""
    from llm.providers.omlx_supervisor import stop_server as _stop
    return await _stop(cfg)


async def _check_single(server: dict) -> dict[str, Any]:
    """Run a per-server health check for the network-server test route."""
    return await check_server({"server_id": server.get("id")})


async def shutdown(_cfg: dict) -> dict[str, Any]:
    """Return the HTTP shutdown unsupported envelope."""
    return {"ok": False, "message": "Shutdown not supported via oMLX HTTP API; signal the process directly"}
