"""Connector DB models.

Connector rows are typed global records keyed by provider. Connector-touching
tools register only when the matching row is ``available``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from connections.vendor_accounts import VENDORS
from infra.database import get_db, write_transaction


class ConnectionType(str, Enum):
    """How CORSIA reaches a connector."""

    CLI = "cli"
    MCP = "mcp"
    API = "api"


class ConnectionStatus(str, Enum):
    """Setup state. Only AVAILABLE registers connector-backed tools.

    UNVERIFIED means the last check timed out or errored. A row whose prior
    state was ready never takes it: it stays AVAILABLE and records the
    unverified result in its ``source_check`` (see ``connections.source_state``).
    """

    AVAILABLE = "available"
    DISABLED = "disabled"
    NOT_INSTALLED = "not_installed"
    NEEDS_AUTH = "needs_auth"
    UNVERIFIED = "unverified"


def _v(x: Any) -> Any:
    """Enum → its ``.value``; pass plain values through."""
    return x.value if isinstance(x, Enum) else x


def _row_to_dict(row) -> dict[str, Any]:
    """sqlite Row → dict with ``config`` JSON-parsed back to a dict (or None)."""
    d = dict(row)
    raw = d.get("config")
    d["config"] = json.loads(raw) if raw else None
    return d


def upsert_connection(*, type, provider: str, status, config: dict | None = None, db=None) -> dict[str, Any]:
    """Insert or update (keyed on ``provider``) a connection row. Returns the row."""
    now = datetime.now(timezone.utc).isoformat()
    status_value = _v(status)
    if provider in VENDORS and config is not None:
        config = {key: value for key, value in config.items() if key != "usage"}
    with write_transaction(db, scope="upsert_connection") as _db:
        previous = _db.execute(
            "SELECT status FROM connections WHERE provider=?", (provider,),
        ).fetchone()
        _db.execute(
            """INSERT INTO connections
               (id, type, provider, config, status, scope, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'global', ?, ?)
           ON CONFLICT(provider) DO UPDATE SET
               type = excluded.type,
               config = excluded.config,
               status = excluded.status,
               updated_at = excluded.updated_at""",
            (
                str(uuid.uuid4()), _v(type), provider,
                json.dumps(config) if config is not None else None,
                status_value, now, now,
            ),
        )
        if provider in VENDORS:
            from connections.harness_legacy_fallback import sync_legacy_fallback
            from connections.vendor_accounts import ensure_default_account

            transport = _db.execute("SELECT id FROM connections WHERE provider=?", (provider,)).fetchone()
            ensure_default_account(str(transport["id"]), db=_db)
            sync_legacy_fallback(
                provider, status=status_value, config=config, db=_db,
            )
            if (
                status_value == ConnectionStatus.AVAILABLE.value
                and previous is not None
                and previous["status"] != ConnectionStatus.AVAILABLE.value
            ):
                from infra.connection_limit_release import release_after_sign_in

                release_after_sign_in(provider, db=_db)
        row = _db.execute("SELECT * FROM connections WHERE provider = ?", (provider,)).fetchone()
        return _row_to_dict(row)


def get_connection(provider: str, db=None) -> dict[str, Any] | None:
    """Return the connection row for ``provider``, or None."""
    _db = db or get_db()
    row = _db.execute("SELECT * FROM connections WHERE provider = ?", (provider,)).fetchone()
    if not db:
        _db.close()
    return _row_to_dict(row) if row else None


def list_connections(*, status=None, type=None, db=None) -> list[dict[str, Any]]:
    """List connections, optionally filtered by status and/or type."""
    _db = db or get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(_v(status))
    if type is not None:
        clauses.append("type = ?")
        params.append(_v(type))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _db.execute(f"SELECT * FROM connections{where} ORDER BY provider", params).fetchall()
    if not db:
        _db.close()
    return [_row_to_dict(r) for r in rows]


def set_connection_status(
    provider: str, status, db=None, *, schedule_usage_check: bool = True,
) -> dict[str, Any] | None:
    """Update a connection's status. Returns the updated row, or None if absent."""
    now = datetime.now(timezone.utc).isoformat()
    status_value = _v(status)
    with write_transaction(db, scope="set_connection_status") as _db:
        previous = _db.execute(
            "SELECT status FROM connections WHERE provider=?", (provider,),
        ).fetchone()
        _db.execute(
            "UPDATE connections SET status = ?, updated_at = ? WHERE provider = ?",
            (status_value, now, provider),
        )
        if provider in ("claude-code", "codex"):
            row = _db.execute(
                "SELECT config FROM connections WHERE provider = ?", (provider,)
            ).fetchone()
            config = json.loads(row["config"]) if row and row["config"] else None
            from connections.harness_legacy_fallback import sync_legacy_fallback

            sync_legacy_fallback(
                provider, status=status_value, config=config, db=_db,
            )
            if (
                status_value == ConnectionStatus.AVAILABLE.value
                and previous is not None
                and previous["status"] != ConnectionStatus.AVAILABLE.value
            ):
                from infra.connection_limit_release import release_after_sign_in

                release_after_sign_in(provider, db=_db, schedule_usage_check=schedule_usage_check)
        row = _db.execute("SELECT * FROM connections WHERE provider = ?", (provider,)).fetchone()
        return _row_to_dict(row) if row else None


def set_harness_enabled(provider: str, enabled: bool, db=None) -> dict[str, Any] | None:
    """Set the one provider-wide on/off state for a CLI harness.

    A disabled connection remains configured but is unavailable to catalog,
    dispatch, and admission. Re-enabling restores the last source-check verdict
    when one exists, so a signed-out harness cannot become available merely
    because its switch was turned back on.
    """
    if provider not in VENDORS:
        raise ValueError(f"{provider!r} is not a harness provider")
    if not enabled:
        return set_connection_status(provider, ConnectionStatus.DISABLED, db=db)

    row = get_connection(provider, db=db)
    if row is None:
        return None
    check = (row.get("config") or {}).get(SOURCE_CHECK_KEY)
    if check and check.get("state"):
        # Keep source-state's existing state-to-status policy canonical.
        from connections.source_state import resolve_check

        status, _ = resolve_check(row, check["state"], None)
    else:
        status = ConnectionStatus.AVAILABLE
    return set_connection_status(provider, status, db=db)


def connection_disabled(provider: str, db=None) -> bool:
    """Whether an operator explicitly turned this provider off."""
    _db = db or get_db()
    row = _db.execute(
        "SELECT status FROM connections WHERE provider = ?", (provider,)
    ).fetchone()
    if not db:
        _db.close()
    return bool(row) and row["status"] == ConnectionStatus.DISABLED.value


def connection_available(provider: str, db=None) -> bool:
    """True iff a connection row exists for ``provider`` with status 'available'.

    This is the gate connection-conditional tools hang off.
    """
    _db = db or get_db()
    row = _db.execute(
        "SELECT status FROM connections WHERE provider = ?", (provider,)
    ).fetchone()
    if not db:
        _db.close()
    return bool(row) and row["status"] == ConnectionStatus.AVAILABLE.value


SOURCE_CHECK_KEY = "source_check"


def record_source_check(
    provider: str, *, status, check: dict[str, Any], db=None, schedule_usage_check: bool = True,
) -> dict[str, Any] | None:
    """Keep a connection's last source check and set its status.

    ``check`` is ``{state, prior_state, reason, checked_at}``; it lives in the
    row's config beside the discovery envelope, so no column is added. The
    status goes through ``set_connection_status``, the one writer of the
    sign-in transition. An explicit disabled state belongs to the person's
    switch, so a check may update its receipt but cannot turn it back on.
    Returns the updated row, or None if the provider has no row.
    """
    with write_transaction(db, scope="record_source_check") as _db:
        row = _db.execute(
            "SELECT status, config FROM connections WHERE provider = ?", (provider,)
        ).fetchone()
        if row is None:
            return None
        config = json.loads(row["config"]) if row["config"] else {}
        config[SOURCE_CHECK_KEY] = check
        _db.execute(
            "UPDATE connections SET config = ? WHERE provider = ?",
            (json.dumps(config), provider),
        )
        effective_status = (
            ConnectionStatus.DISABLED
            if row["status"] == ConnectionStatus.DISABLED.value
            else status
        )
        return set_connection_status(
            provider, effective_status, db=_db, schedule_usage_check=schedule_usage_check,
        )
