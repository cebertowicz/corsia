"""Transactional storage for network endpoint observations."""
from __future__ import annotations

import uuid
from collections.abc import Callable

from infra.database import write_transaction


def store_endpoint(
    server_id: str, host: str, *, kind: str, clock: Callable[[], str], ok: bool, latency_ms: int | None = None,
    supersede: bool = False, generation: int | None = None, db=None,
) -> None:
    """Record a current observation and discard results from an earlier server generation."""
    from llm.server_observation import observation_generation

    with write_transaction(db, scope="upsert_network_endpoint") as connection:
        if generation is not None and observation_generation(server_id) != generation:
            return
        now = clock()
        if ok and supersede:
            connection.execute(
                "UPDATE network_server_endpoints SET last_ok_at = NULL WHERE server_id = ? AND host != ?",
                (server_id, host),
            )
        row = connection.execute(
            "SELECT id FROM network_server_endpoints WHERE server_id = ? AND host = ?", (server_id, host),
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
            connection.execute(f"UPDATE network_server_endpoints SET {', '.join(sets)} WHERE id = ?", vals)
        else:
            eid = f"ep-{uuid.uuid4().hex[:10]}"
            connection.execute(
                """INSERT INTO network_server_endpoints
                       (id, server_id, host, kind, last_ok_at, last_latency_ms,
                        created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, server_id, host, kind, now if ok else None, latency_ms if ok else None, now, now),
            )
