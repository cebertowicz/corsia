"""Provider lifecycle — the public API that routes to the managed engine.

Managed engines (oMLX today) own server + model lifecycle; this module is the
thin façade the app + routes call by provider key. The engine seam itself lives
in ``llm.managed_engine`` — a future platform engine is a new implementation
there, not a change here. External providers (``kind='external'``) are
observe-only and do NOT route through this module.
"""

from __future__ import annotations

from typing import Any

from infra.settings import get_provider_config as _get_provider_config, get_providers
from llm.managed_engine import get_managed_engine, is_managed


def get_provider_config(name: str) -> dict:
    """Load a provider's config from DB settings."""
    return _get_provider_config(name)


def get_local_providers() -> list[dict]:
    """Return all managed-engine provider configs with their names."""
    providers = get_providers()
    return [
        {"name": name, **pcfg}
        for name, pcfg in providers.items()
        if is_managed(name)
    ]


def _cfg_for(provider: str) -> dict:
    """Provider config from settings, with provider name stamped in. The managed
    engine needs this to scope DB lookups when no server_id is in the call."""
    cfg = dict(get_provider_config(provider) or {})
    cfg.setdefault("provider", provider)
    return cfg


async def check_server(provider: str) -> dict[str, Any]:
    """Check if a managed engine's server is running."""
    if not is_managed(provider):
        return {"running": False, "error": f"Unknown provider: {provider}"}
    return await get_managed_engine(provider).health(_cfg_for(provider))


async def start_server(provider: str) -> dict[str, Any]:
    """Start a managed engine's server."""
    if not is_managed(provider):
        return {"ok": False, "message": f"Unknown provider: {provider}"}
    return await get_managed_engine(provider).ensure_live(_cfg_for(provider))


async def autostart_local_providers() -> None:
    """Start each enabled ``autoStart`` managed server not already running."""
    from loguru import logger

    for provider in get_local_providers():
        name = provider.get("name", "")
        if not provider.get("enabled") or not provider.get("autoStart"):
            continue
        try:
            status = await check_server(name)
            if status.get("running"):
                continue
            result = await start_server(name)
            if result.get("ok"):
                logger.info(f"{name} server started via autoStart")
            elif not result.get("requires_model"):
                logger.warning(f"{name} autoStart failed: {result.get('message')}")
        except Exception as exc:
            logger.warning(f"{name} autoStart error: {exc}")


async def stop_server(provider: str) -> dict[str, Any]:
    """Stop a managed engine's server."""
    if not is_managed(provider):
        return {"ok": False, "message": f"Unknown provider: {provider}"}
    return await get_managed_engine(provider).stop(_cfg_for(provider))


async def list_loaded(provider: str) -> list[dict]:
    """List models currently loaded by a managed engine."""
    if not is_managed(provider):
        return []
    return await get_managed_engine(provider).list_loaded(_cfg_for(provider))


async def list_disk_models(provider: str) -> list[dict]:
    """List models available on disk for a managed engine."""
    if not is_managed(provider):
        return []
    return await get_managed_engine(provider).list_disk(_cfg_for(provider))


async def load_model(provider: str, model_key: str, **opts) -> dict[str, Any]:
    """Load a model into a managed engine's server."""
    if not is_managed(provider):
        return {"ok": False, "message": f"Unknown provider: {provider}"}
    return await get_managed_engine(provider).load(_cfg_for(provider), model_key, **opts)


async def unload_model(provider: str, model_key: str) -> dict[str, Any]:
    """Unload a model from a managed engine's server."""
    if not is_managed(provider):
        return {"ok": False, "message": f"Unknown provider: {provider}"}
    return await get_managed_engine(provider).unload(_cfg_for(provider), model_key)


def is_local_provider(name: str) -> bool:
    """Check if a provider name is a managed-engine (local) provider."""
    return is_managed(name)
