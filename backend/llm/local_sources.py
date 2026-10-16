"""Project model-source states from the existing server and catalog owners.

Nothing here probes or keeps check state. Health, discovery and Test updates
are visible on the next read; restarting does not change the projection.
"""

from __future__ import annotations

import re
from typing import Any

from llm.catalog import list_models
from llm.local_setup_ownership import installed_omlx
from llm.network_discovery import get_servers, parse_server_metadata

OMLX_FIX = "Settings › Providers › Local model"
CHECK_AGAIN = "Check again"
_PERCENT = re.compile(r"(\d{1,3})%")


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


def _stage_percent(stage: str) -> int | None:
    """The percent in a setup stage such as "Downloading Model (8.60 GiB, 45%)", else None.

    The percent exists only inside the stage text the flight reports; this is the one
    place it is read, so the app gets a number instead of parsing wording.
    """
    match = _PERCENT.search(stage)
    return min(100, int(match.group(1))) if match else None


def _omlx_state(membership: tuple[set[str], set[str]] | None = None) -> dict[str, Any]:
    """A managed server decides; only with none is this host's binary asked.

    A managed server is registered by host and port, so it may be remote.
    """
    from llm.local_setup import (
        download_estimate, format_size, local_memory_ok, setup_failure, setup_stage, setup_status,
        setup_stopped_following,
    )
    from llm.local_setup_prepare import known_model

    eligible = local_memory_ok()
    model = known_model() if eligible else None
    stage = setup_stage()
    terminal = setup_failure()

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
        result["download_percent"] = _stage_percent(stage)
        eta_seconds, stalled = download_estimate()
        result["download_eta_seconds"] = eta_seconds
        result["download_stalled"] = stalled
        result["setup_downloading"] = setup_status()["state"] == "downloading"
        return result
    name = "Local model"
    servers = [s for s in get_servers(provider="omlx")
               if s.get("kind") != "external" and s.get("status") != "disabled"]
    if not servers:
        if installed_omlx() is None:
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


def _restore_lines(values: dict[str, Any], model: str | None = None) -> list[str]:
    from onboarding import setup_strings as text

    lines = []
    for key, value in sorted(values.items()):
        label = text.OMLX_LABELS.get(key, key)
        shown = text.omlx_value(value)
        lines.append(text.OMLX_RESTORE_MODEL.format(label=label, model=model, value=shown) if model
                     else text.OMLX_RESTORE.format(label=label, value=shown))
    return lines


def omlx_management() -> dict[str, Any]:
    """Whether CORSIA manages the person's own oMLX, and what stopping puts back.

    ``restore_failed`` covers a stop that did not finish, so Settings offers it again.
    ``cannot_check`` is their oMLX's version while CORSIA manages one too old to recommend.
    ``forgets_key``: stopping removes the admin key, which CORSIA stored only to manage it.
    """
    from llm import own_omlx_management

    record = own_omlx_management.read()
    if record is None:
        return {"state": "not_managed", "cannot_check": None, "restores": [], "forgets_key": False}
    model = record.model.model if record.model else None
    restores = [*_restore_lines(record.originals_global),
                *(_restore_lines(record.originals_model, model) if model else [])]
    return {"state": "managed" if record.active else "restore_failed",
            "cannot_check": record.cannot_check if record.active else None, "restores": restores,
            "forgets_key": record.key_registered}


def omlx_source(membership: tuple[set[str], set[str]] | None = None) -> dict[str, Any]:
    """The local source row, with which flight a download or failure belongs to.

    ``omlx_settings`` is set only for the person's own oMLX: None when unknown,
    ``[]`` when its settings match CORSIA's recommended ones, else the differences.
    ``omlx_management`` is likewise only theirs (``omlx_management()``).
    """
    from llm.local_setup import setup_kind
    from llm.local_setup_ownership import existing_install_is_foreign
    from llm.omlx_global_settings import differences, fingerprint

    result = _omlx_state(membership)
    flying = result.get("setup_downloading") or result.get("setup_error")
    result["setup_kind"] = setup_kind() if flying else None
    own = existing_install_is_foreign()
    ids = [s["id"] for s in get_servers(provider="omlx")
           if s.get("kind") != "external" and s.get("status") == "online"] if own else []
    entries = differences(ids) if own and len(ids) == 1 else None
    result["omlx_settings"] = ({"differences": entries, "fingerprint": fingerprint(ids[0], entries)}
                               if entries else entries)
    result["omlx_management"] = omlx_management() if own else None
    return result
