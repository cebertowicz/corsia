"""Cross-backend model status — the single source of "what can run, and how".

The status computation (ready / available / unavailable across native oMLX,
external, cloud-API, and CC/Codex harness backends) lives here so there is ONE
place that decides it (IP36). ``api/models.py::list_models`` returns
:func:`get_model_status_rows` verbatim (plus the active-session count) — the
switcher/catalog surface.
"""

from __future__ import annotations

import json
import re

from connections import fact_memo
from llm.hosted_catalog.connector_models import capabilities_for

_SYNC_HINT = "re-vendor: python scripts/sync_catalog_snapshot.py"
#: Status reason for a harness row whose account reported a usage limit.
USAGE_LIMITED = "usage_limited"
_CLAUDE_IDENTITY_NAME = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)-(?P<minor>\d+)(?:-\d{8})?$"
)


def versioned_name(row: dict, meta: dict) -> str:
    """The discovered name, carrying the version a Claude alias resolves to."""
    from connections.claude_unlisted import base_identity

    identity = meta.get("resolved_identity")
    match = (
        _CLAUDE_IDENTITY_NAME.fullmatch(base_identity(identity))
        if isinstance(identity, str) else None
    )
    if match:
        return f"{match['family'].title()} {match['major']}.{match['minor']}"
    return row["display_name"]


def projected_display_name(row: dict, meta: dict) -> str:
    """The one person-facing catalog name: the person's own label, else the versioned name."""
    return row.get("alias") or versioned_name(row, meta)


def _required_harness_capabilities(harness: str) -> dict:
    """Return harness UI capabilities and fail loudly on snapshot drift."""
    capabilities = capabilities_for(harness)
    if capabilities is None:
        raise RuntimeError(
            f"hosted-catalog snapshot missing capabilities for '{harness}' — "
            f"{_SYNC_HINT}"
        )
    return capabilities


def _harness_connection_available(provider: str) -> bool:
    from connections.models import provider_may_run

    return provider_may_run(provider)


def _harness_discovery_envelope(provider: str) -> dict | None:
    from connections.harness_models import read_discovery_envelope

    return read_discovery_envelope(provider)


def _cloud_providers_active() -> set[str]:
    from infra import settings as _settings
    from infra.key_store_providers import provider_keys

    api_keys = provider_keys()
    cfg_providers = _settings.get_providers()
    active: set[str] = set()
    for prefix, cfg in [("anthropic", "anthropic"), ("openai", "openai")]:
        if not api_keys.get(cfg):
            continue
        if not cfg_providers.get(cfg, {}).get("enabled", True):
            continue
        active.add(prefix)

    return active


def _network_servers() -> list[dict]:
    """Read the network servers once for one catalog build."""
    try:
        from llm.network_discovery import get_servers

        return get_servers()
    except Exception as error:
        from loguru import logger

        logger.warning(f"network discovery lookup failed; treating all servers as offline and non-local: {error}")
        return []


def _network_server_status(servers: list[dict]) -> dict[str, str]:
    """Each network server's current status for one catalog build."""
    return {str(server["id"]): server["status"] for server in servers}


def _server_locality(servers: list[dict]) -> tuple[set[str], set[str]]:
    """Each server's private mark and loopback fact for one catalog build, from the one owner."""
    from llm.locality import server_locality

    private: set[str] = set()
    local: set[str] = set()
    for server in servers:
        facts = server_locality(server)
        if facts.private:
            private.add(str(server["id"]))
        if facts.on_this_mac:
            local.add(str(server["id"]))
    return private, local


def _loaded_model_ids_fn():
    try:
        from llm.network_discovery import get_loaded_model_ids
        return get_loaded_model_ids
    except Exception:
        def _empty(_sid: str) -> set[str]:
            return set()
        return _empty


def _network_status(row: dict, server_status: dict[str, str], loaded_ids_fn) -> str:
    """Status for a network-substrate row (managed ``omlx@`` or ``external@``)."""
    lm = row.get("litellm_model", "")
    kind_prefix, sep, rest = lm.partition("@")
    if not (sep and kind_prefix in ("omlx", "external")):
        return "unavailable"
    sid, _, model_id = rest.partition("/")
    if server_status.get(sid) != "online":
        return "unavailable"
    if model_id in loaded_ids_fn(sid):
        return "ready"
    if row.get("available"):
        return "available"
    return "unavailable"


def _capabilities_for_row(
    harness: str,
    provider: str,
    row: dict,
    meta: dict,
    context_window: int | None,
) -> dict:
    """Build the capability block a switcher row advertises.

    External harnesses report their own discovered effort levels; everything
    else resolves through ``reasoning_config_for``.
    """
    from llm.reasoning_config import reasoning_config_for

    if harness in ("claude-code", "codex"):
        capabilities = dict(_required_harness_capabilities(harness))
        discovered = meta.get("discovered_capabilities") or {}
        levels = discovered.get(
            "effort_levels" if harness == "claude-code" else "reasoning_efforts"
        ) or []
        if levels:
            default = discovered.get("default_reasoning_effort") or "high"
            capabilities["reasoning_config"] = {
                "type": "effort_levels",
                "values": levels,
                "default": default if default in levels else levels[0],
                # These harnesses own an "auto" mode that picks per turn, so the
                # selector keeps offering it. Local models resolve to a concrete
                # server-configured level instead.
                "supports_auto": True,
            }
        capabilities["context_window_max"] = context_window
        return capabilities

    rc = reasoning_config_for(
        provider, row.get("provider_model_id", ""), row.get("litellm_model"),
    )
    # Cloud providers apply their own default when no effort is sent, so the
    # selector keeps its "auto" option for them. Local models report the level
    # they are configured to use, so the picker shows that instead of an unset
    # state.
    if rc is not None and provider in ("openai", "anthropic"):
        rc = {**rc, "supports_auto": True}
    capabilities = {
        "supports_vision": bool(row.get("supports_vision")),
        "supports_tools": bool(row.get("supports_tools")),
        "supports_reasoning": rc is not None,
        "reasoning_config": rc,
        "context_window_max": context_window,
    }
    if provider == "external":
        # No published levels were found for this model: say so rather than
        # presenting "unknown" as "no reasoning".
        capabilities["reasoning_levels_unknown"] = rc is None
    return capabilities


def harness_fields(harness: str, meta: dict) -> dict:
    """The identity, default and alias fields a harness row adds to its entry."""
    fields = {
        "resolved_identity": meta.get("resolved_identity"),
        "is_default": bool(meta.get("is_default")),
    }
    if meta.get("alias_of"):
        # Dispatchable for what is pinned to it; pickers offer the listed row.
        fields["alias_of"] = meta["alias_of"]
    if (_harness_discovery_envelope(harness) or {}).get("last_error"):
        fields["cache_marker"] = "Cached"
    return fields


def harness_connection_for_model(model: str | None, *, db) -> str | None:
    """Resolve a selector through the catalog's target resolver to its harness connection."""
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    try:
        target = resolve_execution_target(model, db=db)
    except (ModelUnavailableError, ValueError):
        return None
    return target.harness if target.harness in ("claude-code", "codex") else None


def _harness_status(
    harness: str,
    row: dict,
    meta: dict,
    context_window: int | None,
    connected: bool,
) -> tuple[str, str | None]:
    """Account-independent status and reason for a Claude Code or Codex row.

    Whether an account can run the model, usage included, is decided per
    account by ``llm.account_catalog.account_model_status``.
    """
    if not connected:
        return "unavailable", "connection_unavailable"
    if not row.get("available"):
        return "unavailable", (
            meta.get("availability_reason")
            or row.get("last_error")
            or "discovered_model_unavailable"
        )
    if not context_window:
        return "unavailable", "missing_context_window"
    return "ready", None


def _public_status_entry(
    row: dict,
    *,
    harness: str,
    provider: str,
    backend: str,
    meta: dict,
    context_window: int | None,
    status: str,
    status_reason: str | None,
    private_servers: set[str],
    local_servers: set[str],
) -> dict:
    """Project one catalog row for every public consumer."""
    capabilities = _capabilities_for_row(
        harness, provider, row, meta, context_window
    )
    display_name = projected_display_name(row, meta)
    if harness == "claude-code" and row.get("source") != "legacy_fallback":
        name = f"Claude Code · {display_name}"
    elif harness == "codex" and row.get("source") != "legacy_fallback":
        name = f"Codex · {display_name}"
    else:
        name = display_name
    entry = {
        "id": row["id"], "selector": row["litellm_model"], "name": name,
        "display_name": display_name, "available": bool(row.get("available")),
        "connection": harness if harness != "native" else provider,
        "desc": row.get("description", ""), "size": "", "backend": backend,
        "provider": provider, "speed": "", "status": status,
        "litellm_model": row["litellm_model"], "context_window": context_window,
        "capabilities": capabilities,
    }
    from llm.locality import row_is_local

    entry["is_local"] = row_is_local(
        entry, private_servers=private_servers, local_servers=local_servers,
    )
    if status_reason:
        entry["status_reason"] = status_reason
    if harness in ("claude-code", "codex"):
        entry.update(harness_fields(harness, meta))
    if backend == "network":
        entry["server_name"] = meta.get("server_name", "")
    return entry


def get_model_status_rows() -> list[dict]:
    """Every selectable model with its live status — the switcher/catalog rows.

    Unscoped is the workspace scope. A harness row's ``status`` is the model's
    own; its ``runs_on`` says what each permitted account can do, and
    ``llm.account_catalog.effective_status`` reads the one the next turn uses.
    """
    return scoped_status_rows()


def scoped_status_rows(*, thread_id: str | None = None, project_id: str | None = None,
                       account_id: str | None = None) -> list[dict]:
    """The catalog rows, each harness row carrying ``runs_on`` for the accounts this scope permits."""
    from llm.account_catalog import project_accounts

    return project_accounts(catalog_base_rows(), thread_id=thread_id, project_id=project_id,
                            account_id=account_id)


@fact_memo.memoized_reader
def harness_base_row(model: str, *, db) -> dict | None:
    """One harness model's account-independent row, hidden or not, for a single-account decision."""
    from llm.harness_catalog import harness_row

    row = harness_row(model, db=db)
    if row is None:
        return None
    from connections.models import provider_may_run
    from llm.context_window import resolve_window_for_row

    row = dict(row)
    status, reason = _harness_status(row["harness"], row, json.loads(row.get("metadata_json") or "{}"),
                                     resolve_window_for_row(row, db=db), provider_may_run(row["harness"], db=db))
    return {"litellm_model": row["litellm_model"], "status": status, "status_reason": reason,
            "default_listed": row.get("default_listed", True)}


def catalog_base_rows() -> list[dict]:
    """Every selectable model, harness rows not yet read on any account."""
    from llm import catalog as model_catalog

    cloud_active = _cloud_providers_active()
    servers = _network_servers()
    server_status = _network_server_status(servers)
    private_servers, local_servers = _server_locality(servers)
    loaded_ids_fn = _loaded_model_ids_fn()
    from llm.harness_catalog import catalog_rows
    rows: list[dict] = []
    connections: dict[str, bool] = {}
    for row in catalog_rows(model_catalog.get_model_status_catalog_rows()):
        entry = _status_entry(row, cloud_active, server_status, loaded_ids_fn, private_servers, local_servers,
                              harness_connections=connections)
        if entry is not None:
            rows.append(entry)
    return rows


def _status_entry(row: dict, cloud_active, server_status, loaded_ids_fn,
                  private_servers, local_servers, *, harness_connections=None) -> dict | None:
    """One catalog row's public entry; ``None`` for an inactive cloud provider."""
    from llm.context_window import resolve_window_for_row

    backend = row["backend"]
    provider = row.get("provider", "")
    harness = row.get("harness", "native")
    context_window = resolve_window_for_row(row)
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
    except Exception:
        meta = {}

    if harness in ("claude-code", "codex"):
        if harness_connections is None:
            connected = _harness_connection_available(harness)
        else:
            if harness not in harness_connections:
                harness_connections[harness] = _harness_connection_available(harness)
            connected = harness_connections[harness]
        status, status_reason = _harness_status(harness, row, meta, context_window, connected)
    elif backend == "api":
        if provider not in cloud_active:
            return None
        status = "ready"
        status_reason = None
    elif backend == "network":
        status = _network_status(row, server_status, loaded_ids_fn)
        status_reason = None
    else:
        status = "unavailable"
        status_reason = None

    if harness == "native" and not context_window:
        status = "unavailable"
        status_reason = "missing_context_window"

    entry = _public_status_entry(
        row, harness=harness, provider=provider, backend=backend, meta=meta,
        context_window=context_window, status=status, status_reason=status_reason,
        private_servers=private_servers, local_servers=local_servers,
    )
    if row.get('default_listed') is False:
        entry['default_listed'] = False
    return entry
