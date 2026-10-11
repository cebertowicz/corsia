"""Mark legacy Alibaba Token Plan endpoints interactive-only."""

from __future__ import annotations

import json
from urllib.parse import urlsplit


def migrate_108_to_109(conn) -> None:
    """Preserve the old endpoint heuristic once as declared endpoint metadata."""
    for row in conn.execute("SELECT id,instance_name,metadata_json FROM network_servers WHERE provider='external'"):
        try:
            metadata = json.loads(row[2] or "{}")
        except (TypeError, ValueError):
            continue
        host = urlsplit(str(metadata.get("base_url") or "")).hostname or ""
        if "token plan" in str(row[1] or "").casefold() or "token-plan" in host.casefold():
            metadata["agent_use_restriction"] = "interactive_only"
            conn.execute("UPDATE network_servers SET metadata_json=? WHERE id=?", (json.dumps(metadata), row[0]))
