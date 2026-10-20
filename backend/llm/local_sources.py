"""Project model-source states from the existing server and catalog owners.

Nothing here probes or keeps check state. Health, discovery and Test updates
are visible on the next read; restarting does not change the projection.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from llm.catalog import list_models
from llm.managed_omlx_binary import managed_omlx_executable
from llm.locality import endpoint_on_this_mac
from llm.network_endpoints import endpoint_base, get_endpoints
from llm.local_setup_executable import omlx_on_path
from llm.local_setup_gates import local_memory_ok
from llm.local_profiles import format_size
from llm.network_discovery import get_servers, parse_server_metadata

OMLX_FIX = "Settings › Providers › Local model"
CHECK_AGAIN = "Check again"
# Download progress comes from the setup flight's structured state.


def catalog_membership() -> tuple[set[str], set[str]]:
    """Available server identities, and those with an enabled available model.

    Two provider-filtered reads, independent of endpoint count, preserve exact
    ``provider@server/`` identity. oMLX needs a listed model; External an enabled
    one. Legacy rows need not have their backend populated.
    """
    available: set[str] = set()
    enabled: set[str] = set()
    rows = [row for provider in ("omlx", "external")
            for row in list_models(provider=provider, include_unavailable=False)]
    for row in rows:
        identity, separator, model = row["litellm_model"].partition("/")
        if not separator or not model:
            continue
        available.add(identity)
        if row["enabled"]:
            enabled.add(identity)
    return available, enabled


def _source(source: str, name: str, state: str, reason: str | None = None,
            fix: str | None = None, checked_at: str | None = None) -> dict[str, Any]:
    return {"source": source, "name": name, "state": state, "prior_state": None,
            "reason": reason, "fix": fix, "checked_at": checked_at}


# Stage copy is presentation-only; callers read structured progress from local_setup.


def _project_local_flight(result: dict[str, Any], flight: dict[str, Any] | None) -> None:
    """Give an unavailable signed-in row the same safe flight fields as Welcome."""
    if flight is not None and result["state"] != "ready":
        result.update(
            local_state=flight["state"],
            setup_error_code=flight.get("cause"),
            setup_error_reference=flight.get("ref"),
            bytes_to_free=flight.get("bytes_to_free"),
        )
        if flight.get("fix"):
            result["fix"] = flight["fix"]


async def local_server_for_install(
    report: Callable[[str], None], *,
    start_managed: Callable[[Callable[[str], None]], Awaitable[tuple[str, int]]] | None = None,
    start_if_absent: bool = True,
) -> tuple[str, int, bool] | None:
    """Resolve the one local server serving this install: connected first, else managed."""
    import asyncio

    connected = await asyncio.to_thread(connected_local_server)
    if connected is not None:
        return str(connected["id"]), int(connected.get("port") or 0), True
    if not start_if_absent:
        return None
    if start_managed is None:
        from llm.local_setup_runtime import start_managed_omlx

        start_managed = start_managed_omlx
    server_id, port = await start_managed(report)
    return server_id, port, False


def local_omlx_servers() -> list[dict]:
    """Every registered oMLX row with a loopback endpoint, regardless of its install origin."""
    return [server for server in get_servers(provider="omlx")
            if server.get("status") != "disabled" and any(
                endpoint_on_this_mac(endpoint_base(
                    parse_server_metadata(server["id"], server.get("metadata_json")) or {},
                    host=ep["host"], port=server.get("port"),
                )) for ep in get_endpoints(server["id"]))]


def connected_local_server() -> dict | None:
    """An existing local server that is online and has a usable catalog model."""
    available, _enabled = catalog_membership()
    return next((server for server in local_omlx_servers()
                 if server.get("status") == "online" and f"omlx@{server['id']}" in available), None)


def _omlx_state(membership: tuple[set[str], set[str]] | None = None) -> dict[str, Any]:
    """Project every local server row; only with none is this host's binary asked."""
    from llm.local_setup import (
        download_estimate, download_percent, setup_failure, setup_kind, setup_stage,
        setup_stopped_following,
    )
    from llm.local_setup_prepare import known_model
    from onboarding.local_download_state import current as local_download_state

    eligible = local_memory_ok()
    model = known_model() if eligible else None
    settings_flight = setup_kind() == "settings"
    flight = None if settings_flight else local_download_state()
    stage = None if settings_flight else setup_stage()
    terminal = None if settings_flight else setup_failure()

    def starter_source(state: str, reason: str | None = None, fix: str | None = None,
                       checked_at: str | None = None) -> dict[str, Any]:
        result = _source("omlx", "Local model", state, reason, fix, checked_at)
        result.update(
            eligible=eligible,
            context_window=model.context_window if model else None,
            download_size=format_size(model.download_size_bytes) if model else None,
        )
        return result

    if stage:
        result = starter_source("needs_setup", stage, OMLX_FIX)
        result["setup_stage"] = stage
        result["download_percent"] = download_percent()
        eta_seconds, stalled = download_estimate()
        result["download_eta_seconds"] = eta_seconds
        result["download_stalled"] = stalled
        result["setup_downloading"] = flight is not None and flight["state"] == "downloading"
        _project_local_flight(result, flight)
        return result
    name = "Local model"
    servers = local_omlx_servers()
    if not servers:
        if managed_omlx_executable() is None and omlx_on_path() is None:
            result = starter_source("not_installed", "oMLX is not installed", "Install oMLX")
        else:
            result = starter_source("needs_setup", "No local model server is set up", OMLX_FIX)
    else:
        online = [s for s in servers if s.get("status") == "online"]
        if not online:
            result = starter_source("needs_setup", "The local model server is not answering",
                                   OMLX_FIX, servers[0].get("updated_at"))
            result["reason_code"] = "not_running"
        else:
            available, _enabled = membership if membership is not None else catalog_membership()
            if not any(f"omlx@{s['id']}" in available for s in online):
                result = starter_source("needs_setup", "The local model server lists no models",
                                       OMLX_FIX, online[0].get("updated_at"))
                result["reason_code"] = "no_models"
            else:
                result = starter_source("ready", checked_at=online[0].get("updated_at"))
    stopped = setup_stopped_following()
    if stopped and result["state"] != "ready":
        result.update(reason=str(stopped["message"]), fix=OMLX_FIX, setup_downloading=True)
        result.pop("reason_code", None)
    if terminal and result["state"] != "ready":
        message = str(terminal.get("message") or "Local setup failed.")
        result.update(reason=message, fix=OMLX_FIX, setup_error=message)
        result.pop("reason_code", None)
    _project_local_flight(result, flight)
    return result


def external_sources(membership: tuple[set[str], set[str]] | None = None) -> list[dict[str, Any]]:
    """One enabled External endpoint per source; preserve the existing state/fix map."""
    _available, enabled = membership if membership is not None else catalog_membership()
    result = []
    for server in get_servers():
        if server.get("kind") != "external" or server.get("status") == "disabled":
            continue
        meta = parse_server_metadata(server["id"], server.get("metadata_json")) or {}
        discovery = meta.get("discovery") or {}
        source, name = f"external:{server['id']}", server.get("instance_name") or server["id"]
        checked_at = discovery.get("at") or server.get("updated_at")
        if server.get("status") == "online" and f"external@{server['id']}" in enabled:
            result.append(_source(source, name, "ready", checked_at=checked_at))
            continue
        reason = (discovery.get("error")
                  or ("The endpoint lists no enabled models" if server.get("status") == "online"
                      else "The endpoint is offline"))
        result.append(_source(source, name, "unverified", reason, CHECK_AGAIN, checked_at))
    return result



def omlx_source(membership: tuple[set[str], set[str]] | None = None) -> dict[str, Any]:
    """The registered local source row and its active download state."""
    from llm.local_setup import download_percent, flight_model, setup_kind, setup_stage

    result = _omlx_state(membership)
    flying = result.get("setup_downloading") or result.get("setup_error")
    current_kind = setup_kind()
    kind = current_kind if flying or current_kind == "settings" else None
    result["setup_kind"] = kind
    # Settings downloads share the durable flight but never borrow setup's
    # fallback semantics. A terminal Settings failure belongs to its durable
    # Settings record so the next sources read can offer the exact retry.
    stage = setup_stage() if kind == "settings" else None
    if stage:
        model = flight_model()
        result["settings_download"] = {
            "name": model.display_name if model else None,
            "repository": model.repository if model else None,
            "percent": download_percent(),
            "failed": False,
        }
    else:
        from llm import local_downloads

        failed = next((entry for entry in local_downloads.all() if entry.status == "failed"), None)
        if failed is not None:
            result["settings_download"] = {
                "name": failed.model.display_name,
                "repository": failed.model.repository,
                "percent": None,
                "failed": True,
            }
    return result
