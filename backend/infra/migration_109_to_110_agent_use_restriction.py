"""Mark legacy Alibaba Token Plan endpoints interactive-only."""

from __future__ import annotations

import json

from llm.endpoint_policy import registration_default


def migrate_109_to_110(conn) -> None:
    """Store the registration default on endpoints that existed before it was applied."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='network_servers'"
    ).fetchone()
    if table is None:
        return
    for row in conn.execute("SELECT id,instance_name,metadata_json FROM network_servers WHERE provider='external'"):
        try:
            metadata = json.loads(row[2] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        default = registration_default(row[1], str(metadata.get("base_url") or ""))
        if default == "interactive_only" and "agent_use_restriction" not in metadata:
            metadata["agent_use_restriction"] = "interactive_only"
            conn.execute("UPDATE network_servers SET metadata_json=? WHERE id=?", (json.dumps(metadata), row[0]))
