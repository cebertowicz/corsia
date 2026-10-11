"""Provider management — API keys and configuration for network + cloud."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.key_session_guard import audit_key_write, require_person_for_key
from api.provider_meta import PROVIDER_META
from api.provider_status import build_provider_status
from api.provider_test import run_provider_test
from infra.auth import require_user
from infra import key_store, key_store_providers, settings
from infra.database import write_transaction
from infra.ignored_environment import ignored_variable
from infra.db_async import run_in_db
from infra.state.ws import notify_models_changed
from llm import catalog as model_catalog

router = APIRouter(
    prefix="/api/settings/providers",
    tags=["providers"],
    # Bearer header or ?token=: agents authenticate by header, never a URL.
    dependencies=[Depends(require_user)],
)


def mask_key(key: str) -> str:
    """Mask an API key, showing only last 4 chars."""
    if not key or len(key) < 8:
        return "••••"
    return "••••" + key[-4:]


def _origin(base: str | None) -> tuple[str, str | None, int] | None:
    """Scheme, host and port a cloud key is sent to; None is the vendor's own endpoint."""
    if not base:
        return None
    parts = urlsplit(base)
    try:
        port = parts.port
    except ValueError:
        raise HTTPException(400, "The base URL's port is not a number") from None
    return parts.scheme, parts.hostname, port or (443 if parts.scheme == "https" else 80)


def _provider_models(provider_prefix: str) -> list[str]:
    """Return model names that use a given provider prefix in litellm_model."""
    return [
        m["display_name"]
        for m in model_catalog.list_models(provider=provider_prefix)
    ]

def _visible_providers(config_providers: dict, keys: dict[str, str]) -> list[dict]:
    """Build the provider list the settings UI renders.

    An unsupported provider is hidden so nobody configures it anew. Hiding one
    that still holds a saved key would strand that credential: the UI would give
    the user no control through which to find or delete it. So a hidden provider
    stays listed while a key exists, flagged ``unsupported`` so the UI can offer
    removal only.
    """
    result = []
    for name, meta in PROVIDER_META.items():
        prov_cfg = config_providers.get(name, {})
        api_key = keys.get(name, "")
        has_key = bool(api_key and api_key.strip())
        unsupported = bool(meta.get("hidden"))
        if unsupported and not has_key:
            continue
        prov_type = meta.get("type", "cloud")

        entry = {
            "name": name,
            "label": meta["label"],
            "type": prov_type,
            "has_key": has_key,
            "unsupported": unsupported,
            "masked_key": mask_key(api_key) if has_key else None,
            "ignored_env_var": ignored_variable(f"provider-{name}"),
            "api_base": prov_cfg.get("apiBase") or meta.get("default_api_base"),
            "docs_url": meta.get("docs_url"),
            "models": _provider_models(name),
        }

        if prov_type == "network":
            entry["enabled"] = prov_cfg.get("enabled", True)
            entry["auto_discover"] = prov_cfg.get("autoDiscover", True)
            entry["capabilities"] = meta.get("capabilities", [])
        else:
            entry["enabled"] = prov_cfg.get("enabled", True)

        result.append(entry)

    result.sort(key=lambda p: (not p["has_key"], p["label"]))
    return result


@router.get("")
async def list_providers():
    """List all known providers with their configuration status."""

    keys = await run_in_db(key_store_providers.provider_keys)
    return {"providers": _visible_providers(settings.get_providers(), keys)}


class ProviderKeyUpdate(BaseModel):
    api_key: str = ""
    api_base: str | None = None
    enabled: bool | None = None
    auto_discover: bool | None = None
    auto_start: bool | None = None


_UNSUPPORTED_PROVIDER_HELP = (
    "{name} is no longer supported: nothing behind it can dispatch a request. "
    "Use the External provider (base URL + token + wire dialect) to reach an "
    "OpenAI-compatible cloud endpoint. Existing saved keys can still be removed "
    "with DELETE."
)


def _refuse_unsupported_provider(name: str, meta: dict) -> None:
    """Reject writes and connectivity tests for a hidden provider.

    Hidden providers stay addressable so a stale saved key can be DELETEd, but
    accepting a new key or reporting a 'test' result for one would be a lie: the
    runtime has no dispatch leg for them, so any real call falls through to the
    poisoned-global 404 recorded in the 2026-02-27 cloud-provider incident.
    """
    if meta.get("hidden"):
        raise HTTPException(400, _UNSUPPORTED_PROVIDER_HELP.format(name=name))


def _save(name: str, value: str | None, update: dict | None) -> None:
    """Store the key and the provider's settings in one transaction, so both land or neither does."""
    with write_transaction(None, scope="provider_save") as db:
        if value is not None:
            try:
                key_store_providers.set_provider_key(name, value, db=db)
            except key_store.KeyStoreError as exc:
                raise HTTPException(400, str(exc)) from None
        if update is not None:
            settings.save_setting_merge("providers", {name: update}, db=db)


@router.put("/{name}")
async def update_provider_key(name: str, body: ProviderKeyUpdate, user: dict = Depends(require_user)):
    """Set or update a provider's cloud or network configuration."""

    if name not in PROVIDER_META:
        raise HTTPException(400, f"Unknown provider: {name}")

    meta = PROVIDER_META[name]
    _refuse_unsupported_provider(name, meta)
    if body.api_key:
        require_person_for_key(user, kind="provider", name=name, action="write")

    if meta.get("type") == "network":
        update: dict = {}
        if body.api_base is not None:
            update["apiBase"] = body.api_base.strip() or meta.get("default_api_base")
        if body.enabled is not None:
            update["enabled"] = body.enabled
        if body.auto_discover is not None:
            update["autoDiscover"] = body.auto_discover
        if body.auto_start is not None:
            update["autoStart"] = body.auto_start
        await run_in_db(_save, name, None, update)
        notify_models_changed()
        return {"ok": True, "name": name, "has_key": False}

    update = {}
    new_key = body.api_key.strip() if body.api_key else None
    if body.api_base is not None:
        update["apiBase"] = body.api_base.strip() or None
    if body.enabled is not None:
        update["enabled"] = body.enabled

    if not update and new_key is None:
        raise HTTPException(400, "Nothing to update")

    incoming_key = new_key if new_key is not None else (await run_in_db(key_store.resolve_provider, name) or "")
    if not incoming_key:
        raise HTTPException(400, "API key required to enable this provider")
    saved_base = (settings.get_providers().get(name) or {}).get("apiBase")
    if new_key is None and "apiBase" in update and _origin(update["apiBase"]) != _origin(saved_base):
        raise HTTPException(
            400, "Enter the API key for the new host. A saved key is only sent to the host it was saved for.",
        )

    await run_in_db(_save, name, new_key or None, update or None)
    if new_key:
        audit_key_write(user, kind="provider", name=name, action="write")

    notify_models_changed()
    return {
        "ok": True,
        "name": name,
        "has_key": bool(incoming_key),
        "masked_key": mask_key(incoming_key),
    }


@router.delete("/{name}")
async def delete_provider_key(name: str, user: dict = Depends(require_user)):
    """Remove a provider's API key: its entry and its saved value."""

    if name not in PROVIDER_META:
        raise HTTPException(400, f"Unknown provider: {name}")
    require_person_for_key(user, kind="provider", name=name, action="clear")

    await run_in_db(key_store_providers.remove_provider_key, name)
    audit_key_write(user, kind="provider", name=name, action="clear")

    notify_models_changed()
    return {"ok": True, "name": name, "has_key": False}


@router.get("/{name}/models")
async def list_provider_models(name: str):
    """List available models for a cloud provider (fetched from API + litellm)."""

    if name not in PROVIDER_META:
        raise HTTPException(400, f"Unknown provider: {name}")

    meta = PROVIDER_META[name]
    if meta.get("type") != "cloud":
        raise HTTPException(400, f"{name} is not a cloud provider")

    api_key = await run_in_db(key_store.resolve_provider, name) or ""
    if not api_key:
        return {"models": [], "error": "No API key configured"}

    from llm.catalog import discover_cloud_models
    return await discover_cloud_models(name, api_key)


@router.post("/{name}/test")
async def test_provider(name: str):
    """Test a provider's connectivity."""

    if name not in PROVIDER_META:
        raise HTTPException(400, f"Unknown provider: {name}")

    meta = PROVIDER_META[name]
    _refuse_unsupported_provider(name, meta)
    return await run_provider_test(name, meta)


@router.get("/{name}/status")
async def provider_status(name: str):
    return await build_provider_status(name)
