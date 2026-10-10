"""Network-server endpoint classification, storage, and selection."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from infra.db_async import run_in_db
from infra.database import get_db, write_transaction

log = logging.getLogger(__name__)

ENDPOINT_FRESHNESS_SECONDS = 35
_RANK = {"local": 0, "lan": 1, "tailscale": 2, "public": 3}


def managed_status_is_healthy(status_payload: dict) -> bool:
    """Whether a managed (oMLX) server's ``/api/status`` body reports health.

    ONE definition, because two of them is how a live server gets declared
    unreachable: the 30s sweep read ``"ok"`` (what oMLX actually sends) while
    the freshness guard read ``"running"`` (what nothing sends), so any gap the
    guard was written to cover — sleep/wake, VPN toggle — ended in a false
    offline flip on a server answering normally.
    """
    return status_payload.get("status") == "ok"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def classify_host(host: str) -> str:
    """Return the endpoint kind for routing preference.

    - ``local``     : 127.0.0.0/8 loopback
    - ``tailscale`` : 100.64.0.0/10 CGNAT (must be checked before is_private —
                      Python 3.11+ counts CGNAT as private)
    - ``lan``       : any other RFC1918 / IPv6-ULA private address
    - ``public``    : everything else, or a non-IP hostname
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "public"

    if ip.is_loopback:
        return "local"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "tailscale"
    if ip.is_private:
        return "lan"
    return "public"


def upsert_endpoint(
    server_id: str,
    host: str,
    *,
    ok: bool,
    latency_ms: int | None = None,
    db=None,
) -> None:
    """Record or refresh a known host for a server.

    Only bumps ``last_ok_at`` when ok is True — a failed probe leaves the
    timestamp stale so freshness-based selection naturally rotates away.
    """
    with write_transaction(db, scope="upsert_network_endpoint") as connection:
        now = _now_iso()
        kind = classify_host(host)

        row = connection.execute(
            "SELECT id FROM network_server_endpoints WHERE server_id = ? AND host = ?",
            (server_id, host),
        ).fetchone()

        if row:
            sets = ["kind = ?", "updated_at = ?"]
            vals: list = [kind, now]
            if ok:
                sets.append("last_ok_at = ?")
                vals.append(now)
                if latency_ms is not None:
                    sets.append("last_latency_ms = ?")
                    vals.append(latency_ms)
            vals.append(row["id"])
            connection.execute(
                f"UPDATE network_server_endpoints SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
        else:
            eid = f"ep-{uuid.uuid4().hex[:10]}"
            connection.execute(
                """INSERT INTO network_server_endpoints
                       (id, server_id, host, kind, last_ok_at, last_latency_ms,
                        created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, server_id, host, kind,
                 now if ok else None, latency_ms if ok else None, now, now),
            )


def get_endpoints(server_id: str) -> list[dict]:
    """All known endpoints for a server (regardless of freshness)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM network_server_endpoints WHERE server_id = ?",
        (server_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_cloud_external(server: dict) -> bool:
    """External with a public endpoint (the UI's Cloud badge); never swept."""
    return is_cloud_server(server.get("kind"), get_endpoints(server["id"]))


def is_cloud_server(kind: str | None, endpoints: list[dict]) -> bool:
    """``is_cloud_external`` over already-read server kind and endpoint rows."""
    return kind == "external" and any(e.get("kind") == "public" for e in endpoints)


def get_active_endpoint(server_id: str) -> dict | None:
    """Preferred endpoint: freshest responder, ranked by kind then latency.

    Returns None if every endpoint is stale (no successful probe within the
    freshness window). Callers that need to force a refresh should invoke
    ``freshness_guard`` first. The window is the sweep's clock, so it does not
    apply to a cloud External server, which is never swept: its endpoint is
    live exactly while its last observation left the server online.
    """
    db = get_db()
    row = db.execute(
        "SELECT kind, status FROM network_servers WHERE id = ?", (server_id,),
    ).fetchone()
    known = get_endpoints(server_id)
    endpoints = [e for e in known if e["last_ok_at"]]
    if row and is_cloud_server(row["kind"], known):
        fresh = endpoints if row["status"] == "online" else []
    else:
        cutoff = (_now() - timedelta(seconds=ENDPOINT_FRESHNESS_SECONDS)).isoformat()
        fresh = [e for e in endpoints if e["last_ok_at"] > cutoff]
    if not fresh:
        return None
    fresh.sort(key=lambda e: (_RANK.get(e["kind"], 99), e["last_latency_ms"] or 9999))
    return fresh[0]


def _server_port(server_id: str) -> int | None:
    db = get_db()
    row = db.execute("SELECT port FROM network_servers WHERE id = ?", (server_id,)).fetchone()
    return int(row["port"]) if row else None


async def probe_endpoint(
    client: httpx.AsyncClient,
    *,
    host: str,
    url: str,
    headers: dict,
    external: bool,
) -> tuple[str, int, dict | None] | None:
    """Probe ONE already-composed URL; the caller's identity stays ``host``.

    Takes a finished URL rather than parts on purpose: composing the URL here would
    put a second owner of the call shape next to :func:`models_probe_url`, and the
    two would drift — which is how an endpoint registered as reachable came to be
    swept as offline.
    """
    try:
        started_at = time.perf_counter()
        response = await client.get(url, headers=headers)
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        if response.status_code != 200:
            return None
        payload = None if external else response.json()
        healthy = external or managed_status_is_healthy(payload or {})
        return (host, latency_ms, payload) if healthy else None
    except (httpx.HTTPError, OSError, ValueError):
        return None


def lan_base(host: str, port: int) -> str:
    """The legacy LAN base managed oMLX and migrated localhost entries use."""
    return f"http://{host}:{port}/v1"


def endpoint_base(
    meta: dict | None, *, host: str, port: int | None, external: bool = False,
) -> str | None:
    """The one reading of a server's call base; None when it has no usable one.

    A stored ``base_url`` IS the base, validated and otherwise untouched. Its
    scheme and path (``https://host/compatible-mode/v1``,
    ``https://host/v1beta/openai``) cannot be recovered from ``host`` + ``port``,
    and the version segment belongs to the provider, so nothing is stripped or
    appended. A stored base that fails validation is None, i.e. unreachable; it
    never falls back to ``http://host:port``, which would probe a different
    server than the one registered. Unreadable metadata (``meta`` None, see
    ``read_server_metadata``) on an ``external`` server is unreachable for the
    same reason: it may have held an https base. Any other server (managed oMLX)
    always served at host:port, so a corrupt blob keeps that base rather than
    making the engine undispatchable. With no stored base this is :func:`lan_base`, except
    for an ``external`` server on a public host: those are registered only by
    URL, so a missing base is unreachable rather than re-derived as plaintext
    ``host:port``. Loopback, LAN and Tailscale External rows registered by
    host:port before URL registration keep the composition they always had.

    Dispatch, the health sweep and the freshness guard all read the base here,
    so for one stored value they cannot disagree about whether the endpoint is
    reachable or which URL it lives at.
    """
    if meta is None:
        return None if external else (lan_base(host, port) if port else None)
    verbatim = str(meta.get("base_url") or "").strip()
    if verbatim:
        return validated_base_url(verbatim)
    if external and classify_host(host) == "public":
        return None
    return lan_base(host, port) if port else None


def anthropic_root(base: str) -> str:
    """``base`` for the native Anthropic SDK, which appends ``/v1/messages`` itself."""
    root = base.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def validated_base_url(url: str) -> str | None:
    """Accept only a scheme-qualified, credential-free URL; refuse everything else.

    Credentials, query and fragment are refused rather than stripped: silently
    dropping a user's ``?key=…`` would hide a mis-entered endpoint, and embedding
    a secret in a URL is not a shape this platform supports.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return url.rstrip("/")


def models_probe_url(base: str, *, dialect: str = "openai") -> str:
    """The one composition of a model-list URL. No caller builds its own.

    An OpenAI-compatible base is the base the OpenAI SDK takes, so its catalog is
    ``<base>/models`` whatever version segment the provider chose (``/v1``,
    ``/v1beta/openai``, ``/api/v3``). The Anthropic API is versioned at
    ``<root>/v1/models``. The registration probe, the health sweep, the freshness
    guard and the catalog fetch all compose here, so they cannot disagree about
    the URL: a disagreement reports a reachable endpoint offline, or registers an
    unreachable one as online.
    """
    if dialect == "anthropic":
        return f"{anthropic_root(base)}/v1/models"
    return f"{base.rstrip('/')}/models"


def chat_request_url(base: str, *, dialect: str = "openai") -> str:
    """Where a turn is sent, composed like :func:`models_probe_url`."""
    if dialect == "anthropic":
        return f"{anthropic_root(base)}/v1/messages"
    return f"{base.rstrip('/')}/chat/completions"


def external_probe_url(
    meta: dict | None, *, dialect: str, host: str, port: int | None
) -> str | None:
    """The model-list URL an External endpoint is probed at; None when it has no base."""
    base = endpoint_base(meta, host=host, port=port, external=True)
    return models_probe_url(base, dialect=dialect) if base else None


def build_api_base(server_id: str) -> str | None:
    """The call base of one server's LIVE endpoint; None when stale or unusable.

    Liveness gates first for every server: a verbatim base URL does not make a
    known-offline endpoint dispatchable. The OpenAI-compatible leg takes this
    as-is; the native Anthropic leg takes :func:`anthropic_root` of it.
    """
    ep = get_active_endpoint(server_id)
    return configured_api_base(server_id, ep["host"]) if ep else None


def configured_api_base(server_id: str, host: str = "") -> str | None:
    """The call base a server is configured at, live or not: for the probe deciding it."""
    from llm.network_discovery import get_server, read_server_metadata

    server = get_server(server_id) or {}
    return endpoint_base(
        read_server_metadata(server_id), host=host, port=_server_port(server_id),
        external=server.get("kind") == "external",
    )


def mgmt_status_url(host: str, port: int) -> str:
    """The managed (oMLX) ``/api/status`` surface for one host — the only place it is built."""
    return f"http://{host}:{port}/api/status"


def build_mgmt_base(server_id: str) -> str | None:
    """Management-API base URL for the current active endpoint, or None if stale."""
    ep = get_active_endpoint(server_id)
    if not ep:
        return None
    port = _server_port(server_id)
    return f"http://{ep['host']}:{port}/api" if port else None


async def freshness_guard(server_id: str, *, budget_s: float = 1.0) -> bool:
    """If the active endpoint is stale/missing, probe all known endpoints in
    parallel and refresh their timestamps. Returns True if at least one
    endpoint is now reachable.

    The 30s sweep keeps timestamps current most of the time; this guard
    closes the gap across network transitions (sleep/wake, LAN leave,
    VPN toggle) by paying a one-time ~200ms tax only when staleness has
    actually happened.
    """
    if await run_in_db(get_active_endpoint, server_id) is not None:
        return True
    endpoints = await run_in_db(get_endpoints, server_id)
    if not endpoints:
        return False

    port = await run_in_db(_server_port, server_id)
    if port is None:
        return False

    # Probe the surface the server's kind actually exposes: managed (oMLX) has
    # the rich /api/status; External is observe-only via the generic /v1/models.
    from llm.network_discovery import get_server, read_server_metadata

    server = await run_in_db(get_server, server_id) or {}
    is_external = server.get("kind") == "external"
    if is_external:
        from llm.external_provider import auth_headers
        stored = await run_in_db(read_server_metadata, server_id)
        meta = stored or {}
        dialect = server.get("dialect") or "openai"
        targets = [
            (ep["host"], external_probe_url(stored, dialect=dialect, host=ep["host"], port=port))
            for ep in endpoints
        ]
        probe_headers = auth_headers(
            server.get("dialect") or "openai", meta.get("api_key"),
        )
    timeout = httpx.Timeout(connect=0.5, read=budget_s, write=budget_s, pool=budget_s)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if is_external:
            results = await asyncio.gather(
                *[
                    probe_endpoint(
                        client, host=host, url=url, headers=probe_headers, external=True,
                    )
                    # An endpoint with no usable base is unreachable, never re-guessed.
                    for host, url in targets
                    if url
                ],
                return_exceptions=True,
            )
        else:
            from llm.providers.omlx_public_probe import probe_registered_statuses
            results = await probe_registered_statuses(client, server_id, endpoints, port)
    for result in results:
        if isinstance(result, tuple):
            host, latency_ms, _payload = result
            await run_in_db(
                upsert_endpoint,
                server_id,
                host,
                ok=True,
                latency_ms=latency_ms,
            )

    reachable = await run_in_db(get_active_endpoint, server_id) is not None
    if not reachable:
        try:
            from llm.network_discovery import update_server_status, _LOADED_BY_SERVER
            await run_in_db(update_server_status, server_id, status="offline")
            _LOADED_BY_SERVER.pop(server_id, None)
        except Exception as e:
            log.warning(f"eager offline-status flip failed for {server_id}: {e}")
    return reachable
