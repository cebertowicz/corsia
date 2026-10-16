"""oMLX settings read/write helpers."""

from __future__ import annotations

from typing import Any

import httpx

from llm.model_metadata import extract_context_length
from llm.providers.omlx_public_auth import public_headers
from llm.providers.omlx_endpoints import (
    _TIMEOUT,
    _api_base,
    ADMIN_KEY_NEEDED,
    NO_ENDPOINT,
    OmlxAdminError,
    admin_request,
)


async def get_settings(cfg: dict) -> dict[str, Any]:
    """Read server-wide admin settings for display."""
    try:
        resp = await admin_request(cfg, "GET", "/global-settings")
    except OmlxAdminError as e:
        if str(e) in (ADMIN_KEY_NEEDED, NO_ENDPOINT):
            return {"ok": True, "settings": {}, "readonly": True}
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if resp.status_code == 200:
        return {"ok": True, "settings": resp.json() or {}, "readonly": True}
    return {"ok": False, "message": f"HTTP {resp.status_code}"}


async def update_settings(_cfg: dict, _body: Any) -> dict[str, Any]:
    """Reject writes to CLI-owned server-wide settings."""
    return {
        "ok": False,
        "message": (
            "Server-wide oMLX settings are configured via oMLX CLI flags "
            "(--max-process-memory, --paged-ssd-cache-dir, --hot-cache-max-size). "
            "Use per-model settings for runtime model controls."
        ),
    }


async def save_global_settings(cfg: dict, pairs: dict[str, Any]) -> dict[str, Any]:
    """The sole oMLX global-settings writer, returning only safe status data."""
    try:
        response = await admin_request(cfg, "POST", "/global-settings", json=pairs)
    except OmlxAdminError as e:
        message = str(e) if str(e) == ADMIN_KEY_NEEDED else "oMLX is unreachable"
        return {"ok": False, "status": 503, "message": message}
    except Exception:
        return {"ok": False, "status": 503, "message": "oMLX is unreachable"}
    if response.status_code != 200:
        # CORSIA's own status: oMLX's 401 must not read as the caller's sign-in failing.
        return {"ok": False, "status": 502,
                "message": f"oMLX could not save settings (oMLX HTTP {response.status_code})"}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    applied = payload.get("runtime_applied", []) if isinstance(payload, dict) else []
    applied = applied if isinstance(applied, list) else []
    return {"ok": True, "runtime_applied": [key for key in applied if key in pairs]}


async def get_model_config(cfg: dict, model_id: str) -> dict[str, Any]:
    """Read per-model generation config for the model details UI."""
    try:
        resp = await admin_request(cfg, "GET", f"/models/{model_id}/generation_config")
    except OmlxAdminError as e:
        if str(e) in (ADMIN_KEY_NEEDED, NO_ENDPOINT):
            return {"ok": True, "config": {}}
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if resp.status_code == 200:
        return {"ok": True, "config": resp.json() or {}}
    return {"ok": False, "message": f"HTTP {resp.status_code}"}


async def get_model_runtime_info(cfg: dict, model_id: str) -> dict[str, Any]:
    """Read public runtime state from ``GET /v1/models/status``."""
    api_base = _api_base(cfg)
    if not api_base:
        return {"ok": False, "message": "Server has no reachable endpoint"}
    try:
        headers = await public_headers(cfg, url=f"{api_base}/models/status")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{api_base}/models/status", headers=headers)
            if resp.status_code != 200:
                return {"ok": False, "message": f"HTTP {resp.status_code}"}
            for m in (resp.json().get("models") or []):
                if m.get("id") == model_id:
                    return {
                        "ok": True,
                        "loaded": bool(m.get("loaded")),
                        "pinned": m.get("pinned"),
                        "last_access": m.get("last_access"),
                        "engine_type": m.get("engine_type"),
                        "context_length": extract_context_length(m),
                        "thinking_default": m.get("thinking_default"),
                        "preserve_thinking_default": m.get("preserve_thinking_default"),
                    }
            return {"ok": False, "message": f"Model not found: {model_id}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


_OMLX_FIELD_ALIASES = {
    "pinned": "is_pinned",
    "idle_timeout_seconds": "ttl_seconds",
}
_OMLX_FIELD_ALIASES_REVERSE = {v: k for k, v in _OMLX_FIELD_ALIASES.items()}


async def get_model_settings(cfg: dict, model_id: str) -> dict[str, Any]:
    """Read per-model admin settings, and the model's ``thinking_default``, from the model list endpoint."""
    try:
        resp = await admin_request(cfg, "GET", "/models")
        if resp.status_code != 200:
            return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json() or {}
        models = data if isinstance(data, list) else data.get("models", [])
        for m in models:
            if m.get("id") == model_id:
                raw = m.get("settings") or {}
                public = {
                    _OMLX_FIELD_ALIASES_REVERSE.get(k, k): v
                    for k, v in raw.items()
                }
                return {"ok": True, "settings": public, "thinking_default": m.get("thinking_default")}
        return {"ok": False, "message": f"Model not found: {model_id}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


async def update_model_settings(
    cfg: dict, model_id: str, settings: dict[str, Any],
) -> dict[str, Any]:
    """Update per-model admin settings.

    A value sent as None (JSON null) resets that field to its own default; an
    omitted field is left as it is. A failure carries oMLX's ``status``;
    ``model_missing`` is oMLX's own answer that it has no such model. Both are
    upstream oMLX's ``PUT /admin/api/models/{model_id}/settings``
    (github.com/jundot/omlx, tag v0.7.1rc1, ``omlx/admin/routes.py`` 2691-2730:
    ``model_fields_set`` at 2730 tells a sent null from an omitted field, and an
    unknown model is the 404 ``Model not found: {model_id}`` at 2723).
    """
    wire = {_OMLX_FIELD_ALIASES.get(k, k): v for k, v in settings.items()}
    try:
        resp = await admin_request(cfg, "PUT", f"/models/{model_id}/settings", json=wire)
        if resp.status_code == 200:
            resp_json = resp.json() or {}
            if "settings" in resp_json and isinstance(resp_json["settings"], dict):
                raw = resp_json["settings"]
                resp_json["settings"] = {
                    _OMLX_FIELD_ALIASES_REVERSE.get(k, k): v for k, v in raw.items()
                }
            return {"ok": True, **resp_json}
        return {"ok": False, "status": resp.status_code,
                "model_missing": resp.status_code == 404 and _detail(resp) == f"Model not found: {model_id}",
                "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _detail(resp: Any) -> object:
    """FastAPI's error ``detail`` from an oMLX response, else None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None
