"""Mark legacy Alibaba Token Plan endpoints interactive-only."""

from __future__ import annotations

import json
from urllib.parse import urlsplit


def migrate_109_to_110(conn) -> None:
    """Preserve the retired endpoint heuristic once as declared metadata."""
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
        host = urlsplit(str(metadata.get("base_url") or "")).hostname or ""
        matches_legacy_endpoint = (
            "token plan" in str(row[1] or "").casefold() or "token-plan" in host.casefold()
        )
        if matches_legacy_endpoint and "agent_use_restriction" not in metadata:
            metadata["agent_use_restriction"] = "interactive_only"
            conn.execute("UPDATE network_servers SET metadata_json=? WHERE id=?", (json.dumps(metadata), row[0]))
