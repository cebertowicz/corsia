"""Minting, listing, revoking and resolving measure keys (schema in ``infra.measure_keys_schema``).

A key is ``mlk_`` plus 32 random bytes as unpadded URL-safe base64 (43 characters). Only its
SHA-256 is stored; the plaintext is returned once, at mint. A key is deliberately not an
``auth_tokens`` row: that table's tokens are full account credentials, and this one may only
write declared events for one agent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from infra.database import write_transaction

KEY_PREFIX = "mlk_"
KEY_SHAPE = re.compile(r"mlk_[A-Za-z0-9_-]{43}")
_LABEL_MAX = 80


@dataclass(frozen=True)
class KeyOwner:
    """What a key resolves to on the server; the payload never supplies any of it."""

    key_id: str
    user_id: str
    agent_thread_id: str


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def mint(user_id: str, agent_thread_id: str, label: str, *, db: sqlite3.Connection | None = None) -> tuple[str, str]:
    """Create a key for ``agent_thread_id``; returns ``(key_id, plaintext)``. The plaintext is never stored."""
    label = label.strip()
    if not label or len(label) > _LABEL_MAX:
        raise ValueError(f"label must be 1 to {_LABEL_MAX} characters")
    key = KEY_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    key_id = uuid.uuid4().hex[:12]
    with write_transaction(db, scope="mint_measure_key") as conn:
        conn.execute(
            "INSERT INTO measure_keys (key_id,key_hash,user_id,agent_thread_id,label,created_at) VALUES (?,?,?,?,?,?)",
            (key_id, digest(key), user_id, agent_thread_id, label, datetime.now(timezone.utc).isoformat()),
        )
    return key_id, key


def list_keys(db: sqlite3.Connection, agent_thread_id: str) -> list[dict[str, Any]]:
    """The agent's keys with ids, labels and state; never a key or its hash."""
    rows = db.execute(
        "SELECT key_id,label,created_at,revoked_at FROM measure_keys WHERE agent_thread_id=? ORDER BY created_at",
        (agent_thread_id,),
    )
    return [{"key_id": r[0], "label": r[1], "created_at": r[2], "revoked": r[3] is not None} for r in rows]


def revoke(agent_thread_id: str, key_id: str, *, db: sqlite3.Connection | None = None) -> bool:
    """Revoke one of the agent's own keys. False when it has no such live key."""
    with write_transaction(db, scope="revoke_measure_key") as conn:
        cursor = conn.execute(
            "UPDATE measure_keys SET revoked_at=? WHERE key_id=? AND agent_thread_id=? AND revoked_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), key_id, agent_thread_id),
        )
        return cursor.rowcount == 1


def resolve(db: sqlite3.Connection, key: str) -> KeyOwner | None:
    """The owner of a live key, or None for an unknown or revoked one. Read-only."""
    wanted = digest(key)
    row = db.execute(
        "SELECT key_id,user_id,agent_thread_id,key_hash FROM measure_keys WHERE key_hash=? AND revoked_at IS NULL",
        (wanted,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row[3], wanted):
        return None
    return KeyOwner(row[0], row[1], row[2])
