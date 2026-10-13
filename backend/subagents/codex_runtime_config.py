from __future__ import annotations

from infra.database import get_db


class CodexConnectionUnavailable(RuntimeError):
    """Raised before spawn when the Codex CLI connection is not usable."""


def codex_connection(db=None) -> dict:
    _db = db or get_db()
    try:
        from connections.models import ConnectionStatus, get_connection
        conn = get_connection("codex", db=_db)
        status = (conn or {}).get("status")
        if not conn or status != ConnectionStatus.AVAILABLE.value:
            reason = status or "missing"
            raise CodexConnectionUnavailable(
                f"Codex connection is not available: {reason}"
            )
        if not bool(conn.get("enabled", 1)):
            raise CodexConnectionUnavailable("Codex connection is not available: provider_disabled")
        return conn
    finally:
        if not db:
            _db.close()


def ensure_connection_available(db=None) -> None:
    codex_connection(db)


def codex_binary(conn: dict) -> str:
    return ((conn or {}).get("config") or {}).get("binary", "codex")
