"""Persist and log the latest External provider discovery outcome."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from infra.database import write_transaction
from infra.key_store_servers import resolve_server
from llm.network_discovery import parse_server_metadata, set_server_metadata

log = logging.getLogger(__name__)


def record_external_discovery(
    server_id: str, result: dict, *, model_count: int, db=None,
    target: tuple | None = None,
) -> dict:
    """Record one External discovery outcome in its owning transaction.

    ``target`` is what a discovery that reached the endpoint fetched with. An
    outcome an endpoint edit has since replaced is not written, whether it is a
    snapshot or a failure (see :func:`superseded_by_edit`).
    """
    with write_transaction(db, scope="record_external_discovery") as connection:
        if superseded_by_edit(connection, server_id, target, result):
            return result
        summary = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "models": model_count,
            "error": result.get("error"),
            # Why reasoning levels could not be refreshed, if they could not.
            "reasoning_levels_error": result.get("reasoning_levels_error"),
        }
        set_server_metadata(server_id, db=connection, discovery=summary)
    if summary["error"]:
        log.warning("external discovery for %s failed: %s", server_id, summary["error"])
    else:
        log.info("external discovery for %s found %d models", server_id, summary["models"])
    return result


def superseded_by_edit(db, server_id: str, target: tuple | None, result: dict) -> bool:
    """True, with ``result`` marked, when an edit committed after discovery read its target.

    ``target`` is the (dialect, base URL, key) the discovery fetched with. A
    snapshot taken from the old URL or key must not overwrite the catalog, or
    the discovery status, that the edit's own discovery is about to write.

    The stored base is read through :func:`endpoint_base`, the same reading
    dispatch and discovery use, so the two cannot disagree about where the
    endpoint now lives.
    """
    from llm.network_endpoints import endpoint_base

    if target is None:
        return False
    row = db.execute(
        "SELECT dialect, port, metadata_json FROM network_servers WHERE id = ?", (server_id,),
    ).fetchone()
    if not row:
        return False
    meta = parse_server_metadata(server_id, row["metadata_json"])
    dialect, api_base, key = target
    # The host matters only to a legacy host:port row (an IP literal), whose
    # base is lan_base(host, port); a stored base_url ignores it.
    host = urlsplit(api_base).hostname or ""
    current = endpoint_base(meta, host=host, port=row["port"], external=True)
    if ((row["dialect"] or "openai") == dialect and resolve_server(server_id) == key
            and current == api_base):
        return False
    result["error"] = "Superseded by an endpoint edit"
    log.info("external discovery for %s superseded by an endpoint edit", server_id)
    return True


def switched_off(db, server_id: str, result: dict) -> bool:
    """True, with ``result`` marked, when the server was disabled or removed meanwhile.

    Its rows were withdrawn when it was switched off; a snapshot read before
    that must not make them available again.
    """
    row = db.execute(
        "SELECT status FROM network_servers WHERE id = ?", (server_id,),
    ).fetchone()
    if row is None or row["status"] not in ("disabled", "removed"):
        return False
    result["error"] = "Server is switched off"
    return True


async def finish_external_discovery(
    server_id: str, result: dict, *, model_count: int = 0, target: tuple | None = None,
) -> dict:
    """Record an early or failed discovery exit in a new, owned DB job.

    The outcome is still returned when recording it fails, so a broken
    database cannot also hide the discovery error from the caller.
    """
    from infra.db_async import run_in_db

    try:
        return await run_in_db(
            record_external_discovery, server_id, result,
            model_count=model_count, target=target,
        )
    except Exception as exc:
        log.error("could not record external discovery for %s: %s", server_id, exc)
        return result
