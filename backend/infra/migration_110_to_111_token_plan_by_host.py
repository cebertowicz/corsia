"""Clear the agent-use restriction from endpoints that are not on a Token Plan host."""

from __future__ import annotations

import json

from llm.endpoint_policy import registration_default


def migrate_110_to_111(conn) -> None:
    """Unmark external endpoints whose host is not a Token Plan host.

    Migration 109→110 and registration first recognised a Token Plan endpoint by its
    name or its host; they now read the host only. An install that ran the earlier
    rule can hold ``interactive_only`` on an endpoint whose host is not a Token Plan
    host, because of the name it had when it was marked. That name may since have
    changed, since re-adding the same base URL overwrites it while the stored
    restriction survives, so the current name cannot tell which marks the name rule
    made. This removes ``interactive_only`` from every external endpoint off a Token
    Plan host, so it reads as one that declared nothing, the state a fresh install
    stores for it. Endpoints on a Token Plan host are left alone.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_servers'"
    ).fetchone()
    if table is None:
        return
    rows = conn.execute(
        "SELECT id,metadata_json FROM network_servers WHERE provider='external'"
    ).fetchall()
    for server_id, raw in rows:
        try:
            metadata = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict) or metadata.get("agent_use_restriction") != "interactive_only":
            continue
        if registration_default(str(metadata.get("base_url") or "")) == "interactive_only":
            continue
        del metadata["agent_use_restriction"]
        conn.execute("UPDATE network_servers SET metadata_json=? WHERE id=?", (json.dumps(metadata), server_id))
