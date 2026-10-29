"""Project model-source states from the existing server and catalog owners.

Nothing here probes or keeps check state. Health, discovery and Test updates
are visible on the next read; restarting does not change the projection.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from llm.catalog import list_models
from llm.managed_omlx_binary import is_corsia_managed_server, managed_omlx_executable, managed_omlx_install_error
from llm.locality import endpoint_on_this_mac
from llm.network_endpoints import endpoint_base, get_endpoints
from llm.local_setup_executable import omlx_on_path
from llm.local_setup_gates import host_never_runs_local, local_setup_memory_ok
from llm.local_profiles import format_size
from llm.network_discovery import get_servers, parse_server_metadata

OMLX_FIX = "Settings › Providers › Local model"
CHECK_AGAIN = "Check again"
NEVER_LOCAL_REASON = "Local models don’t run on this computer"


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


def _project_local_flight(result: dict[str, Any], flight: dict[str, Any] | None) -> None:
    """Project the local setup flight as the row's only lifecycle vocabulary."""
    if flight is None or flight["state"] == "stopped":
        result["local_state"] = "stopped"
        return
    result.update(
        local_state=flight["state"],
        setup_error_code=flight.get("cause"),
        setup_error_reference=flight.get("ref"),
        bytes_to_free=flight.get("bytes_to_free"),
        download_percent=flight.get("percent"),
        download_eta_seconds=flight.get("eta_seconds"),
        download_stalled=flight.get("stalled") is True,
        fix=flight.get("fix"),
    )


async def local_server_for_install(
    report: Callable[[str], None], *,
    start_managed: Callable[[Callable[[str], None]], Awaitable[tuple[str, int]]] | None = None,
    start_if_absent: bool = True,
) -> tuple[str, int, bool] | None:
    """Resolve the one local server for this install: CORSIA's own oMLX, started when absent."""
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


def corsia_omlx_server(
    servers: list[dict] | None = None, *, include_disabled: bool = False,
) -> dict | None:
    """CORSIA's own oMLX server row, online or not; None when it isn't registered.

    Identity only: a caller that needs it running also reads ``status``. ``UNIQUE(provider,
    instance_id)`` allows at most one such row, so there is no choice between several. By
    default a disabled server remains unavailable to existing callers; callers that need to
    explain that state can opt in. Pass the oMLX rows a caller already read to avoid reading
    them again.
    """
    rows = get_servers(provider="omlx") if servers is None else servers
    return next((server for server in rows
                 if is_corsia_managed_server(server)
                 and (include_disabled or server.get("status") != "disabled")), None)


def local_omlx_servers(servers: list[dict] | None = None) -> list[dict]:
    """Every registered oMLX row with a loopback endpoint, regardless of its install origin.

    Pass the oMLX rows a caller already read to avoid reading them again.
    """
    return [server for server in (get_servers(provider="omlx") if servers is None else servers)
            if server.get("status") != "disabled" and any(
                endpoint_on_this_mac(endpoint_base(
                    parse_server_metadata(server["id"], server.get("metadata_json")) or {},
                    host=ep["host"], port=server.get("port"),
                )) for ep in get_endpoints(server["id"]))]


def installed_managed_server() -> dict | None:
    """Return CORSIA's managed server when its catalog has an installed model."""
    server = corsia_omlx_server(include_disabled=True)
    if server is None:
        return None
    prefix = f"omlx@{server['id']}/"
    return server if any(row["litellm_model"].startswith(prefix)
                         for row in list_models(provider="omlx", include_unavailable=True)) else None


def connected_local_server() -> dict | None:
    """CORSIA's own oMLX when it is online and has a usable catalog model.

    A person's own oMLX is never this install's server, however many models it lists.
    """
    server = corsia_omlx_server()
    if server is None or server.get("status") != "online":
        return None
    available, _enabled = catalog_membership()
    return server if f"omlx@{server['id']}" in available else None


def _omlx_state(membership: tuple[set[str], set[str]] | None = None,
                omlx_rows: list[dict] | None = None) -> dict[str, Any]:
    """Project every local server row; only with none is this host's binary asked."""
    from llm.local_setup import setup_kind
    from llm.local_setup_prepare import known_model
    from onboarding.local_download_state import current as local_download_state

    never_local = host_never_runs_local()
    eligible = local_setup_memory_ok() and not never_local
    model = known_model() if eligible else None
    settings_flight = setup_kind() == "settings"
    flight = None if settings_flight else local_download_state()

    def starter_source(state: str, reason: str | None = None, fix: str | None = None,
                       checked_at: str | None = None) -> dict[str, Any]:
        result = _source("omlx", "Local model", state, reason, fix, checked_at)
        result.update(
            # Permission to set up or add a model, not permission to run an installed one.
            eligible=eligible,
            never_local=never_local,
            context_window=model.context_window if model else None,
            download_size=format_size(model.download_size_bytes) if model else None,
        )
        return result

    if never_local:
        result = starter_source("unverified", NEVER_LOCAL_REASON)
        _project_local_flight(result, None)
        return result

    name = "Local model"
    servers = local_omlx_servers(omlx_rows)
    installed = installed_managed_server() if not eligible else None
    if installed is not None and all(server["id"] != installed["id"] for server in servers):
        servers.append(installed)
    if not servers:
        install_error = managed_omlx_install_error()
        if install_error:
            result = starter_source("needs_setup")
            _project_local_flight(result, {"state": "failed", "cause": "damaged_bundle", "fix": install_error})
            return result
        if managed_omlx_executable() is None and omlx_on_path() is None:
            result = starter_source("not_installed", "oMLX is not installed", "Install oMLX")
        else:
            result = starter_source("needs_setup", "No local model server is set up", OMLX_FIX)
    else:
        online = [s for s in servers if s.get("status") == "online"]
        if not online:
            result = starter_source("needs_setup", "The local model server is not answering",
                                   OMLX_FIX, servers[0].get("updated_at"))
            result["reason_code"] = "not_running" if eligible or installed else "no_models"
        else:
            available, _enabled = membership if membership is not None else catalog_membership()
            if not any(f"omlx@{s['id']}" in available for s in online):
                result = starter_source("needs_setup", "The local model server lists no models",
                                       OMLX_FIX, online[0].get("updated_at"))
                result["reason_code"] = "no_models"
            else:
                result = starter_source("ready", checked_at=online[0].get("updated_at"))
    if not settings_flight:
        # A finished setup whose server has since stopped was turned off, not still finishing.
        turned_off = flight is not None and flight["state"] == "ready" and result.get("reason_code") == "not_running"
        _project_local_flight(result, flight if result["state"] != "ready" and not turned_off else None)
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
    from llm.local_setup import download_percent, flight_model, setup_finishing, setup_kind, setup_stage

    servers = get_servers(provider="omlx")
    result = _omlx_state(membership, servers)
    if result.get("never_local") is True:
        result.update(corsia_omlx=False, corsia_omlx_online=False, setup_kind=None)
        return result
    own = corsia_omlx_server(servers)
    # Identity apart from status, as on the server rows: Set up shows only without CORSIA's own oMLX.
    result["corsia_omlx"] = own is not None
    result["corsia_omlx_online"] = own is not None and own.get("status") == "online"
    flying = result.get("local_state") in {"downloading", "starting", "failed", "setup_failed", "start_failed"}
    current_kind = setup_kind()
    kind = current_kind if flying or current_kind == "settings" else None
    result["setup_kind"] = kind
    # A Settings download, and a setup that has picked its model, are the same
    # download to the person: both show on the oMLX row and in the Add model
    # sheet. Settings never borrows setup's fallback semantics, and a terminal
    # Settings failure belongs to its durable Settings record so the next
    # sources read can offer the exact retry.
    shows_download = kind == "settings" or (current_kind == "setup" and flight_model() is not None)
    stage = setup_stage() if shows_download else None
    if stage:
        model = flight_model()
        result["settings_download"] = {
            "name": model.display_name if model else None,
            "repository": model.repository if model else None,
            "size_bytes": (model.resident_bytes or model.download_size_bytes) if model else None,
            "percent": download_percent(),
            "failed": False,
            "finishing": setup_finishing(),
        }
    else:
        from llm import local_downloads

        # Discovery of CORSIA's oMLX forgets a failure once it lists the model.
        failed = next((entry for entry in local_downloads.all() if entry.status == "failed"), None)
        if failed is not None:
            result["settings_download"] = {
                "name": failed.model.display_name,
                "repository": failed.model.repository,
                "percent": None,
                "failed": True,
                "downloaded": failed.downloaded,
            }
    return result
