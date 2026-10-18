"""oMLX runtime interactions reused by the local setup flight."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

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


class OptimalResult:
    """Best-effort outcome from the benchmark settings pass."""

    def __init__(self, status: str, speed: float | None = None, *, writer_attempted: bool = False) -> None:
        self.status = status
        self.speed = speed
        self.writer_attempted = writer_attempted


def _model_specific_fields(payload: object) -> set[str]:
    """Read the current oMLX model-specific allowlist without copying it locally."""
    if not isinstance(payload, dict):
        return set()
    values = payload.get("model_specific")
    if not isinstance(values, list):
        values = payload.get("model_specific_fields")
    return {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()


async def _without_uninstalled_drafts(server_id: str, settings: dict[str, Any]) -> dict[str, Any] | None:
    """Drop draft-model settings whose referenced model is absent from this oMLX."""
    drafts = {key: value for key, value in settings.items()
              if key.endswith("_draft_model") and isinstance(value, str)}
    if not drafts:
        return settings
    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    try:
        response = await admin_request({"server_id": server_id}, "GET", "/models", timeout=15.0)
        payload = response.json() if response.status_code == 200 else None
    except (OmlxAdminError, ValueError):
        return None
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    installed = {row.get("id") for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return {key: value for key, value in settings.items() if key not in drafts or value in installed}


async def model_settings_payload(server_id: str, model: LocalModel) -> dict[str, Any]:
    """The one CORSIA-owned per-model settings payload; it never includes sampling."""
    return {
        **MODEL_SETTINGS,
        "max_context_window": model.context_window,
        **({"mtp_enabled": True} if model.mtp else {}),
        **await thinking_settings(server_id, model),
    }


async def _read_optimal_inputs(server_id: str, model: LocalModel) -> tuple[dict[str, Any], object, object, object] | OptimalResult:
    """Read the candidate, benchmark and current oMLX state without writing."""
    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    try:
        candidate = await admin_request(
            {"server_id": server_id}, "GET", f"/models/{model.model}/settings/optimal", timeout=15.0,
        )
        if candidate.status_code == 404:
            return OptimalResult("not_found")
        if candidate.status_code != 200:
            return OptimalResult("offline")
        payload = candidate.json()
        rows = payload.get("by_tg") if isinstance(payload, dict) else None
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        benchmark_id = row.get("benchmark_id") if row is not None else None
        if not isinstance(benchmark_id, str) or not benchmark_id:
            return OptimalResult("not_found")
        async with httpx.AsyncClient(timeout=10.0) as client:
            benchmark = await client.get(f"https://omlx.ai/api/benchmarks/{benchmark_id}")
        if benchmark.status_code != 200:
            return OptimalResult("offline")
        fields = await admin_request({"server_id": server_id}, "GET", "/profile-fields", timeout=15.0)
        current = await admin_request({"server_id": server_id}, "GET", f"/models/{model.model}/settings", timeout=15.0)
        if fields.status_code != 200 or current.status_code != 200:
            return OptimalResult("offline")
        return row, benchmark.json(), fields.json(), current.json()
    except OmlxAdminError:
        return OptimalResult("no_omlx")
    except (httpx.HTTPError, ValueError):
        return OptimalResult("offline")


async def apply_optimal_settings(server_id: str, model: LocalModel) -> OptimalResult:
    """Apply only compatible benchmark performance fields in one model-settings write."""
    from llm import own_omlx_management
    from llm.local_model import omlx_has_recommender
    from llm.local_setup_auth import omlx_version
    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    if own_omlx_management.uses_mine():
        return OptimalResult("no_omlx")
    if not omlx_has_recommender(await asyncio.to_thread(omlx_version)):
        return OptimalResult("unsupported")
    inputs = await _read_optimal_inputs(server_id, model)
    if isinstance(inputs, OptimalResult):
        return inputs
    row, snapshot, fields, current = inputs
    settings = snapshot.get("model_settings") if isinstance(snapshot, dict) else None
    allowed = _model_specific_fields(fields)
    if not isinstance(settings, dict) or not isinstance(current, dict):
        return OptimalResult("offline")
    filtered = {key: value for key, value in settings.items() if key in allowed}
    # TurboQuant is an all-or-nothing feature; a disabled snapshot must not carry
    # its stale bit/skip fields into this model.
    if filtered.get("turboquant_kv_enabled") is not True:
        filtered = {key: value for key, value in filtered.items() if not key.startswith("turboquant_")}
    compatible = await _without_uninstalled_drafts(server_id, filtered)
    if compatible is None:
        return OptimalResult("offline")
    if not compatible:
        return OptimalResult("not_found")
    filtered = compatible
    if own_omlx_management.active():
        own_omlx_management.record_originals({}, {model.model: {key: current.get(key) for key in filtered}})
    try:
        corsia = await model_settings_payload(server_id, model)
    except RuntimeError:
        return OptimalResult("offline")
    try:
        response = await admin_request(
            {"server_id": server_id}, "PUT", f"/models/{model.model}/settings",
            json={**corsia, **filtered}, timeout=15.0,
        )
    except (httpx.HTTPError, OmlxAdminError):
        return OptimalResult("offline", writer_attempted=True)
    if response.status_code != 200:
        return OptimalResult("offline", writer_attempted=True)
    speed = row.get("tg_tps") if isinstance(row, dict) else None
    return OptimalResult("applied", float(speed) if isinstance(speed, (int, float)) else None)


async def configure_model_settings(server_id: str, model: LocalModel) -> None:
    """Set the picked model's oMLX settings.

    The window is the recommender's; the model-independent settings come from
    ``local_profiles.json``; sampling is left unset so the model's own generation
    config applies.
    """
    from llm.providers.omlx_endpoints import admin_request

    payload = await model_settings_payload(server_id, model)
    response = await admin_request(
        {"server_id": server_id}, "PUT", f"/models/{model.model}/settings", json=payload, timeout=15.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"oMLX could not save settings for {model.display_name} (HTTP {response.status_code}).")
