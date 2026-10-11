"""Clear the agent-use restriction that an endpoint's name alone put there."""

from __future__ import annotations

import json

from llm.endpoint_policy import registration_default


def migrate_110_to_111(conn) -> None:
    """Unmark endpoints restricted only because their name said "Token Plan".

    Migration 109→110 and registration first recognised a Token Plan endpoint by its
    name or its host; they now read the host only. An install that ran the earlier
    rule can hold ``interactive_only`` on an endpoint whose host is not a Token Plan
    host, purely because of its free-text name. This removes that value, so the
    endpoint reads as one that declared nothing, the state a fresh install stores
    for it. Endpoints on a Token Plan host, and names without "Token Plan", are
    left alone.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_servers'"
    ).fetchone()
    if table is None:
        return
    rows = conn.execute(
        "SELECT id,instance_name,metadata_json FROM network_servers WHERE provider='external'"
    ).fetchall()
    for server_id, name, raw in rows:
        if "token plan" not in (name or "").casefold():
            continue
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
