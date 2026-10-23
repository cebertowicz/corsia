from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from infra.database import get_db, write_transaction
from llm.hosted_catalog.loader import CatalogSnapshotError
from llm.reasoning_config import reasoning_shape_from_metadata

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enrich_from_litellm(model_id: str, provider: str) -> dict:
    try:
        import litellm
        mc = litellm.model_cost.get(model_id, {})
        if not mc:
            mc = litellm.model_cost.get(f"{provider}/{model_id}", {})
        ctx = mc.get("max_input_tokens") or mc.get("max_tokens")
        if ctx is None:
            log.warning(
                f"litellm.model_cost has no context window for "
                f"{provider}/{model_id} — DB will store NULL and the "
                f"resolver will raise. Fix: extend litellm's model_cost "
                f"table OR use the provider's native SDK to discover "
                f"per-model max_input_tokens directly."
            )
        return {
            "input_cost_per_1m": round((mc.get("input_cost_per_token", 0) or 0) * 1e6, 2),
            "output_cost_per_1m": round((mc.get("output_cost_per_token", 0) or 0) * 1e6, 2),
            "context_window": ctx,
            "max_output_tokens": mc.get("max_output_tokens") or mc.get("max_tokens"),
            "supports_vision": bool(mc.get("supports_vision")),
            "supports_tools": bool(mc.get("supports_function_calling", True)),
            "supports_reasoning": bool(mc.get("supports_reasoning")),
        }
    except Exception as exc:
        log.warning(
            f"_enrich_from_litellm raised for {provider}/{model_id}: {exc}. "
            f"DB will store NULL context_window; resolver will raise."
        )
        return {
            "input_cost_per_1m": 0,
            "output_cost_per_1m": 0,
            "context_window": None,
            "max_output_tokens": None,
            "supports_vision": False,
            "supports_tools": True,
            "supports_reasoning": False,
        }


def _is_chat_model_openai(model_id: str) -> bool:
    low = model_id.lower()
    for skip in ("dall-e", "tts", "whisper", "text-embedding", "babbage", "davinci",
                 "chatgpt-", "omni-moderation", "sora", "computer-use",
                 "realtime", "transcribe", "audio", "image",
                 "search-preview", "search-api", "instruct"):
        if skip in low:
            return False
    if "-202" in model_id:
        return False
    if low.endswith("-pro"):
        return False
    if "codex" in low:
        return False
    if "chat-latest" in low:
        return False
    if "deep-research" in low:
        return False
    return True


def _is_featured_model(model_id: str, created: int) -> bool:
    import time
    age_days = (time.time() - created) / 86400
    return age_days < 120


def _openai_discovered(api_models: list[dict]) -> list[dict]:
    discovered: list[dict] = []
    for model in api_models:
        model_id = model.get("id", "")
        if not _is_chat_model_openai(model_id):
            continue
        display = (
            model_id.replace("gpt-", "GPT-").replace("o3", "O3").replace("o4", "O4")
        )
        discovered.append({
            "provider_model_id": model_id,
            "litellm_model": f"openai/{model_id}",
            "display_name": display,
            "featured": _is_featured_model(model_id, model.get("created", 0)),
            **_enrich_from_litellm(model_id, "openai"),
        })
    return discovered


def _anthropic_discovered() -> list[dict]:
    import litellm

    discovered: list[dict] = []
    for key in litellm.model_cost:
        if (
            not key.startswith("claude-")
            or "-202" in key
            or "instant" in key
            or "claude-2" in key
            or "claude-3-" in key
        ):
            continue
        discovered.append({
            "provider_model_id": key,
            "litellm_model": f"anthropic/{key}",
            "display_name": key.replace("claude-", "Claude ").replace("-", " ").title(),
            "featured": True,
            **_enrich_from_litellm(key, "anthropic"),
        })
    return discovered


def _cloud_model_metadata(
    model: dict, stored_metadata: str | None,
    fallback: dict[str, Any] | CatalogSnapshotError | None,
) -> str:
    metadata: dict[str, Any] = {
        "input_cost_per_1m": model["input_cost_per_1m"],
        "output_cost_per_1m": model["output_cost_per_1m"],
    }
    reasoning = reasoning_shape_from_metadata(stored_metadata)
    if reasoning is None:
        if isinstance(fallback, CatalogSnapshotError):
            raise fallback
        reasoning = fallback
    if reasoning is not None:
        metadata["reasoning_shape"] = reasoning
    return json.dumps(metadata)


def _upsert_cloud_model(db, provider: str, model: dict, now: str, fallback) -> dict:
    short_id = f"{provider}-{model['provider_model_id']}"
    existing = db.execute(
        "SELECT id, enabled, metadata_json FROM models "
        "WHERE provider = ? AND provider_model_id = ?",
        (provider, model["provider_model_id"]),
    ).fetchone()
    metadata = _cloud_model_metadata(model, existing["metadata_json"] if existing else None, fallback)
    max_output_tokens = model.get("max_output_tokens")
    if existing:
        db.execute(
            """UPDATE models SET display_name = ?, context_window = ?,
               supports_vision = ?, supports_tools = ?, supports_reasoning = ?,
               default_max_tokens = ?, default_temperature = NULL,
               metadata_json = ?, available = 1,
               updated_at = ? WHERE id = ?""",
            (model["display_name"], model["context_window"],
             int(model["supports_vision"]), int(model["supports_tools"]),
             int(model["supports_reasoning"]), max_output_tokens, metadata,
             now, existing["id"]),
        )
    else:
        enabled = 1 if model["featured"] else 0
        db.execute(
            """INSERT INTO models
               (id, provider, provider_model_id, litellm_model, display_name,
                backend, source, enabled, available, context_window,
                supports_vision, supports_tools, supports_reasoning,
                default_temperature, default_max_tokens, metadata_json,
                discovered_at, last_seen_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,NULL,?,?,?,?,?,?)""",
            (short_id, provider, model["provider_model_id"], model["litellm_model"],
             model["display_name"], "api", "cloud_discovered", enabled,
             model["context_window"], int(model["supports_vision"]),
             int(model["supports_tools"]), int(model["supports_reasoning"]),
             max_output_tokens, metadata, now, now, now, now),
        )
    return {
        "id": model["provider_model_id"],
        "db_id": existing["id"] if existing else short_id,
        "display_name": model["display_name"],
        "enabled": existing["enabled"] if existing else int(model["featured"]),
        **{
            key: model[key]
            for key in (
                "input_cost_per_1m", "output_cost_per_1m", "context_window",
                "supports_vision", "supports_tools", "supports_reasoning",
            )
        },
    }


def _persist_cloud_models(provider: str, discovered: list[dict], now: str) -> list[dict]:
    from llm.hosted_catalog.cloud_models import reasoning_shape_for

    shapes: list[dict[str, Any] | CatalogSnapshotError | None]
    try:
        shapes = [reasoning_shape_for(provider, model["provider_model_id"]) for model in discovered]
    except CatalogSnapshotError as error:
        # Saved shapes win; raise a failed fallback only for a row that needs it.
        shapes = [error] * len(discovered)
    with write_transaction(None, scope="persist_cloud_models") as db:
        models = [
            _upsert_cloud_model(db, provider, model, now, shape)
            for model, shape in zip(discovered, shapes)
        ]
    log.info("Refreshed cloud catalog: %s (%d models)", provider, len(models))
    return models


def _provider_rows(provider: str) -> list[tuple]:
    """A provider's stored model rows minus refresh timestamps, to tell a real change from a re-read."""
    rows = get_db().execute(
        "SELECT * FROM models WHERE provider = ? ORDER BY id", (provider,)
    ).fetchall()
    skip = {"updated_at", "last_seen_at"}
    return [tuple(row[key] for key in row.keys() if key not in skip) for row in rows]


async def discover_cloud_models(provider: str, api_key: str) -> dict:
    import httpx

    now = _now()
    result: dict = {"ok": False, "models": []}

    if provider == "openai":
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if response.status_code != 200:
                    result["error"] = f"API returned {response.status_code}"
                    return result
                api_models = response.json().get("data", [])
        except Exception as exc:
            result["error"] = str(exc)
            return result
        discovered = _openai_discovered(api_models)

    elif provider == "anthropic":
        try:
            discovered = _anthropic_discovered()
        except Exception as exc:
            result["error"] = str(exc)
            return result

    else:
        result["error"] = f"Cloud discovery not implemented for {provider}"
        return result

    before = _provider_rows(provider)
    result["models"] = _persist_cloud_models(provider, discovered, now)
    result["ok"] = True
    from infra.state.ws import notify_models_changed

    if _provider_rows(provider) != before:
        notify_models_changed()
    result["models"].sort(key=lambda model: (-model.get("enabled", 0), model["display_name"]))
    return result
