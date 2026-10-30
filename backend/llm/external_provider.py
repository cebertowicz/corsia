"""External (user-owned) inference providers.

An External provider is a name + base-URL/IP + auth token + wire ``dialect``
(``openai`` | ``anthropic``) — covering localhost, LAN, and cloud endpoints
CORSIA talks to but does NOT manage. It is **observe-only**: CORSIA never
loads or unloads an external model; the user runs the server. This is the sole
counterpart to the Managed engine (oMLX); the bespoke llama.cpp / LM Studio
lifecycle code is retired in favour of these generic rows.

Substrate is shared with managed servers (``network_servers`` +
``network_server_endpoints``); the ``kind='external'`` + ``dialect`` columns are
the only divergence. Dispatch reuses the existing router legs:
``openai/<id>`` + ``api_base`` → litellm OpenAI-compat; ``anthropic/<id>`` +
``api_base`` → the native Anthropic SDK (which already accepts ``base_url``).
"""

from __future__ import annotations

import json as _json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from infra.database import write_transaction
from infra.key_store_servers import resolve_server

log = logging.getLogger(__name__)

PROVIDER = "external"
PREFIX = "external@"
DIALECTS = ("openai", "anthropic")
_ANTHROPIC_VERSION = "2023-06-01"

# litellm's model table keys each provider's models under its own namespace.
# The endpoint's host is the only evidence of which provider serves a model --
# the dialect is just the wire format -- so a window is looked up only where the
# host names the provider, and only by exact model id. Anything else stays
# unknown rather than borrowing another provider's number.
_REFERENCE_NAMESPACES: dict[str, tuple[str, ...]] = {
    "aliyuncs.com": ("dashscope",),
}


def catalog_model_id(server_id: str, pmid: str) -> str:
    """A row's ``provider_model_id``: qualified by its endpoint.

    The catalog keys rows by ``(provider, provider_model_id)`` and every External
    endpoint shares ``provider='external'``, so two endpoints serving the same
    model (a vendor's pay-as-you-go key and its plan key) need distinct keys. The
    id the endpoint itself serves is the tail of ``litellm_model``."""
    return f"{server_id}/{pmid}"


def served_model_id(litellm_model: str) -> str:
    """The model id the endpoint serves, from ``external@<server_id>/<id>``."""
    return litellm_model.removeprefix(PREFIX).partition("/")[2]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def auth_headers(dialect: str, api_key: str | None) -> dict[str, str]:
    """Auth headers for an external endpoint's ``/v1/models`` probe + discovery.

    OpenAI-dialect uses ``Authorization: Bearer``; Anthropic-dialect uses
    ``x-api-key`` + the required ``anthropic-version`` header. Returns ``{}``
    when no key is configured (open localhost endpoints need none)."""
    if dialect == "anthropic":
        headers = {"anthropic-version": _ANTHROPIC_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def build_external_params(server_id: str, provider_model_id: str) -> dict:
    """Build ``chat_completion`` params for an external model.

    Routes by the server's ``dialect``: ``anthropic`` → the native SDK leg with
    a custom ``base_url``; ``openai`` (default) → the litellm OpenAI-compat leg.
    Fails loud when the server row or a reachable endpoint is missing."""
    from llm.reasoning_config import listed_effort_levels
    from llm.network_discovery import get_server
    from llm.network_endpoints import anthropic_root, build_api_base

    server = get_server(server_id)
    if not server:
        raise ValueError(f"External server not found: {server_id}")
    dialect = server.get("dialect") or "openai"
    api_key = resolve_server(server_id)

    if dialect == "anthropic":
        # The SDK appends /v1/messages itself, so it takes the bare root.
        base = build_api_base(server_id)
        root = anthropic_root(base) if base else None
        if not root:
            raise ValueError(f"External server has no reachable endpoint: {server_id}")
        return {
            "model": f"anthropic/{provider_model_id}",
            # A NON-EMPTY key is REQUIRED even for a keyless endpoint: with none, the
            # SDK falls back to the ANTHROPIC_API_KEY environment variable, which would
            # send CORSIA's own cloud credential to this user-supplied endpoint. The
            # sentinel forecloses that (same as the openai leg below and oMLX's "omlx").
            "api_key": api_key or "sk-noauth",
            "api_base": root,
            "uses_streaming": True,
        }

    api_base = build_api_base(server_id)
    if not api_base:
        raise ValueError(f"External server has no reachable endpoint: {server_id}")
    return {
        "model": f"openai/{provider_model_id}",
        # A NON-EMPTY key is required: litellm's openai route falls back to the
        # OPENAI_API_KEY env var when api_key is None/empty, which would misroute
        # a keyless local endpoint to OpenAI's cloud (or throw AuthError). Use a
        # sentinel for keyless endpoints; same pattern as oMLX ("omlx") and the
        # retired LM Studio backend ("lm-studio").
        "api_key": api_key or "sk-noauth",
        "api_base": api_base,
        "uses_streaming": True,
        "reasoning_levels": listed_effort_levels(f"{PREFIX}{server_id}/{provider_model_id}"),
    }


def _capability_overrides(server: dict) -> dict:
    """Per-server capability defaults the user supplied at add time.

    Generic ``/v1/models`` carries no capability metadata and often no context
    window, so External models inherit these user-supplied defaults (HMC11:
    unknown models get truth from catalog OR user override). Stored in
    ``metadata_json``."""
    from llm.external_declared_models import declared_model_ids
    from llm.network_discovery import get_server_metadata

    meta = get_server_metadata(server["id"])
    return {
        "context_window": meta.get("context_window"),
        "supports_vision": 1 if meta.get("supports_vision") else 0,
        "supports_tools": 0 if meta.get("supports_tools") is False else 1,
        "supports_reasoning": 1 if meta.get("supports_reasoning") else 0,
        "declared_models": declared_model_ids(meta),
    }


def reference_context_window(host: str, model_id: str) -> int | None:
    """The provider-published input window for ``model_id`` on ``host``, if known."""
    host = (host or "").lower().rstrip(".")
    namespaces = next(
        (ns for suffix, ns in _REFERENCE_NAMESPACES.items()
         if host == suffix or host.endswith("." + suffix)),
        (),
    )
    if not namespaces:
        return None
    import litellm

    for namespace in namespaces:
        window = (litellm.model_cost.get(f"{namespace}/{model_id}") or {}).get("max_input_tokens")
        if isinstance(window, int) and window > 0:
            return window
    return None


async def fetch_external_models(
    api_base: str, dialect: str, api_key: str | None
) -> dict[str, int | None]:
    """The model ids an external endpoint lists via ``/v1/models``, in order, each
    with the context window its entry reports (vLLM's ``max_model_len``), or None."""
    from llm.model_metadata import extract_context_length
    from llm.network_endpoints import models_probe_url

    headers = auth_headers(dialect, api_key)
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(models_probe_url(api_base, dialect=dialect), headers=headers)
        resp.raise_for_status()
        data = resp.json() or {}
    listed: dict[str, int | None] = {}
    for m in (data.get("data") or []):
        mid = m.get("id") if isinstance(m, dict) else None
        if isinstance(mid, str) and mid:
            listed[mid] = extract_context_length(m)
    return listed


def _external_discovery_config(
    server_id: str,
) -> tuple[dict | None, str | None, str | None, dict | None]:
    """Cloud discovery is its own probe (configured base); others wait for the sweep."""
    from llm.network_discovery import get_server
    from llm.network_endpoints import build_api_base, configured_api_base, is_cloud_external

    server = get_server(server_id)
    cloud = bool(server and is_cloud_external(server))
    if not server or server.get("status") not in (("online", "offline") if cloud else ("online",)):
        return server, None, None, None
    return (
        server,
        configured_api_base(server_id) if cloud else build_api_base(server_id),
        resolve_server(server_id),
        _capability_overrides(server),
    )


def _upsert_external_model(
    db,
    server_id: str,
    server: dict,
    dialect: str,
    pmid: str,
    caps: dict,
    now: str,
    host: str = "",
    shapes: dict[str, dict | None] | None = None,
    reported_window: int | None = None,
) -> str:
    litellm_model = f"{PREFIX}{server_id}/{pmid}"
    # The user's per-server window wins; then the one this server reports for the
    # model; otherwise the provider's published one.
    window = (caps.get("context_window") or reported_window
              or reference_context_window(host, pmid))
    display_name = pmid.split("/")[-1] or pmid
    metadata = {
        "server_id": server_id,
        "server_name": server.get("instance_name", ""),
        "dialect": dialect,
    }
    if shapes and shapes.get(pmid):
        metadata["reasoning_shape"] = shapes[pmid]
    # Sampling and output limits are the provider's settings: the row stores
    # none, and refresh clears the 0.3/8192 older discovery wrote.
    # Keyed by transport string, so rows written with the bare id before ids were
    # qualified by endpoint are adopted and re-keyed rather than duplicated.
    existing = db.execute(
        "SELECT id, metadata_json FROM models WHERE provider = ? AND litellm_model = ?",
        (PROVIDER, litellm_model),
    ).fetchone()
    if existing:
        from llm.external_image_input import image_input_state

        # Image support was detected or chosen per model; rediscovery keeps it.
        image_input = image_input_state(existing["metadata_json"])
        if image_input:
            metadata["image_input"] = image_input
        if shapes is None:  # the level catalog was unreadable: keep what was read last
            from llm.external_reasoning import keep_stored_shape

            keep_stored_shape(existing["metadata_json"], metadata)
        db.execute(
            """UPDATE models SET available = 1, provider_model_id = ?,
               display_name = ?, context_window = COALESCE(?, context_window),
               default_temperature = NULL, default_max_tokens = NULL,
               metadata_json = ?, last_seen_at = ?, updated_at = ? WHERE id = ?""",
            (catalog_model_id(server_id, pmid), display_name, window,
             _json.dumps(metadata), now, now, existing["id"]),
        )
        return "updated"

    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            description, backend, source, enabled, available, context_window,
            supports_vision, supports_tools, supports_reasoning,
            default_temperature, default_max_tokens,
            metadata_json, discovered_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,1,1,?,?,?,?,NULL,NULL,?,?,?,?,?)""",
        ("ext-" + str(uuid.uuid4())[:8], PROVIDER,
         catalog_model_id(server_id, pmid), litellm_model,
         display_name, f"External · {dialect}", "network", "discovered",
         window, caps["supports_vision"], caps["supports_tools"],
         caps["supports_reasoning"], _json.dumps(metadata), now, now, now, now),
    )
    return "discovered"


def _reconcile_stale_external_models(
    db,
    server_id: str,
    seen: set[str],
    now: str,
) -> int:
    from infra.settings import get_host_access_reviewer_model

    stale = db.execute(
        "SELECT id, litellm_model, source FROM models "
        "WHERE provider = ? AND litellm_model LIKE ?",
        (PROVIDER, f"{PREFIX}{server_id}/%"),
    ).fetchall()
    configured_reviewer = get_host_access_reviewer_model(db=db)
    missing = 0
    for row in stale:
        if served_model_id(row["litellm_model"]) in seen:
            continue
        if row["source"] == "discovered" and row["id"] != configured_reviewer:
            db.execute("DELETE FROM models WHERE id = ?", (row["id"],))
        else:
            db.execute(
                "UPDATE models SET available = 0, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        missing += 1
    return missing


def _reconcile_external_models(
    server_id: str,
    server: dict,
    dialect: str,
    model_ids: list[str],
    caps: dict,
    now: str,
    result: dict,
    host: str = "",
    target: tuple | None = None,
    shapes: dict[str, dict | None] | None = None,
    reported_windows: dict[str, int | None] | None = None,
) -> dict:
    """Apply one authoritative External snapshot in the DB worker pool."""
    from llm import external_discovery_result as outcome

    seen = set(model_ids)
    with write_transaction(None, scope="reconcile_external_models") as db:
        if (outcome.superseded_by_edit(db, server_id, target, result)
                or outcome.switched_off(db, server_id, result)):
            return result
        for pmid in model_ids:
            result[_upsert_external_model(
                db, server_id, server, dialect, pmid, caps, now, host, shapes,
                (reported_windows or {}).get(pmid),
            )] += 1
        result["missing"] = _reconcile_stale_external_models(
            db, server_id, seen, now,
        )
        result["ok"] = True
        outcome.record_external_discovery(server_id, result, model_count=len(model_ids), db=db)
    return result


async def discover_external_models(server_id: str) -> dict:
    """Discover an external provider's models and upsert catalog rows.

    Observe-only: every listed model is recorded as available. Capability facts
    come from the user-supplied per-server overrides (generic ``/v1/models`` has
    none); a context window an entry reports is kept. Mirrors the managed
    discovery's stale-row cleanup."""
    from infra.db_async import run_in_db
    from llm.external_declared_models import discoverable_model_ids
    from llm.external_discovery_result import finish_external_discovery
    from llm.external_reasoning import discovery_shapes
    from llm.server_observation import observe_discovery

    result: dict = {"ok": False, "discovered": 0, "updated": 0, "missing": 0}
    server, api_base, api_key, caps = await run_in_db(
        _external_discovery_config, server_id,
    )

    if not server:
        result["error"] = f"Server not found: {server_id}"
        return await finish_external_discovery(server_id, result)
    if caps is None:
        result["error"] = "Server is offline"
        return await finish_external_discovery(server_id, result)

    if not api_base:
        result["error"] = "Server has no reachable endpoint"
        return await finish_external_discovery(server_id, result)

    dialect = server.get("dialect") or "openai"
    # Past this point the outcome describes this endpoint as fetched; an edit
    # committed meanwhile supersedes it, failures included.
    target = (dialect, api_base, api_key)
    try:
        windows = await discoverable_model_ids(
            api_base, dialect, api_key, (caps or {}).get("declared_models") or [],
        )
        model_ids = list(windows)
    except Exception as exc:
        result["error"] = f"Connection failed: {exc}"
        await observe_discovery(server, None, exc)
        return await finish_external_discovery(server_id, result, target=target)

    shapes, levels_error = await discovery_shapes(dialect, api_base, model_ids)
    if levels_error:
        result["reasoning_levels_error"] = levels_error
    try:
        result = await run_in_db(
            _reconcile_external_models,
            server_id,
            server,
            dialect,
            model_ids,
            caps or {},
            _now(),
            result,
            urlsplit(api_base).hostname or "",
            target,
            shapes,
            windows,
        )
    except Exception as exc:
        log.error("external model discovery DB error: %s", exc)
        result["ok"] = False
        result["error"] = str(exc)
        return await finish_external_discovery(server_id, result, target=target)
    await observe_discovery(server, model_ids if result["ok"] else None)
    return result
