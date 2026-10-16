"""oMLX runtime interactions reused by the local setup flight."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx

from llm.local_model import LocalModel
from llm.local_profiles import MODEL_SETTINGS, OMLX_GLOBAL_SETTINGS, thinking_recommendation
from llm.local_setup_auth import SKIP_AUTH_SETTING
from llm.local_setup_ownership import omlx_log_detail

LOCAL_HOST = "127.0.0.1"
HEALTH_WAIT_SECONDS = 120.0
LOCAL_SERVER_NAME = "This Mac"


async def omlx_answers(port: int) -> bool:
    """Whether oMLX answers its health check on ``port`` right now."""
    from llm.providers.omlx_endpoints import health_url

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.get(health_url(LOCAL_HOST, port))).status_code == 200
    except httpx.HTTPError:
        return False


async def wait_for_health(port: int) -> None:
    """Wait for oMLX on ``port``; on timeout, say what oMLX's own log says."""
    from llm.providers.omlx_endpoints import health_url

    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(health_url(LOCAL_HOST, port))).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    detail = await asyncio.to_thread(omlx_log_detail)
    raise RuntimeError(
        f"oMLX didn't answer on {LOCAL_HOST}:{port} within {int(HEALTH_WAIT_SECONDS)} seconds.{detail}",
    )


async def register_loopback_server(*, port: int, api_key: str | None = None) -> str:
    """Register a healthy loopback oMLX through the existing manual path."""
    from api.network_server_helpers import _add_omlx_server

    result = await _add_omlx_server(SimpleNamespace(
        hostname=LOCAL_HOST, port=port, name=LOCAL_SERVER_NAME, api_key=api_key,
    ))
    return str(result["server_id"])


async def configure_global(server_id: str) -> None:
    """Write oMLX's server-wide settings and loopback-only auth flag."""
    from llm.providers.omlx_settings import save_global_settings

    result = await save_global_settings(
        {"server_id": server_id}, {**OMLX_GLOBAL_SETTINGS, SKIP_AUTH_SETTING: True},
    )
    if not result.get("ok"):
        raise RuntimeError(str(result.get("message", "oMLX could not save global settings.")))


async def thinking_settings(server_id: str, model: LocalModel) -> dict:
    """What an agent turn on ``model`` needs written so it thinks (``thinking_recommendation``)."""
    from llm.providers.omlx_endpoints import admin_request

    response = await admin_request({"server_id": server_id}, "GET", "/models", timeout=15.0)
    if response.status_code != 200:
        raise RuntimeError(f"oMLX could not list its models (HTTP {response.status_code}).")
    payload = response.json()
    rows = payload.get("models") if isinstance(payload, dict) else None
    row = next((r for r in rows or [] if isinstance(r, dict) and r.get("id") == model.model), None)
    if row is None:
        raise RuntimeError(f"oMLX does not list {model.display_name} after its download.")
    return thinking_recommendation(row.get("thinking_default"))


async def configure_model_settings(server_id: str, model: LocalModel) -> None:
    """Set the picked model's oMLX settings.

    The window is the recommender's; the model-independent settings come from
    ``local_profiles.json``; sampling is left unset so the model's own generation
    config applies.
    """
    from llm.providers.omlx_endpoints import admin_request

    payload = {
        **MODEL_SETTINGS, "max_context_window": model.context_window,
        **({"mtp_enabled": True} if model.mtp else {}),
        **await thinking_settings(server_id, model),
    }
    response = await admin_request(
        {"server_id": server_id}, "PUT", f"/models/{model.model}/settings", json=payload, timeout=15.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"oMLX could not save settings for {model.display_name} (HTTP {response.status_code}).")
