"""The shared vendor-account row shape, extracted to keep ``vendor_accounts`` in budget.

Every account reader in ``connections.vendor_accounts`` selects the same columns, joins
the same connection row for the provider, and hands callers the same dict. Keeping that
projection here leaves the reads and writes with ``connections.vendor_accounts`` while
keeping that module below its line budget after the fact-reader decorators were added.

``COLUMNS`` deliberately excludes subscription usage, whose only SQL owner is
``connections.harness_usage``: a row read here must never carry a usage blob.
"""
from __future__ import annotations

import json
from typing import Any

# Explicit projection excludes subscription usage, whose only SQL owner is harness_usage.
COLUMNS = ",".join("a." + name for name in (
    "id", "connection_id", "name", "source_home", "runtime_home", "is_default", "state", "revision",
    "identity_json", "auth_cleanup_json", "discovery_json", "created_at", "updated_at", "removed_at",
)) + ",c.provider"

JOIN = " FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id"


def project_row(row) -> dict[str, Any]:
    """One selected row as the dict every account reader returns, JSON fields parsed."""
    account = dict(row)
    account["identity"] = json.loads(account.pop("identity_json"))
    cleanup = account.pop("auth_cleanup_json")
    account["auth_cleanup"] = json.loads(cleanup) if cleanup else None
    raw = account.pop("discovery_json")
    account["discovery"] = json.loads(raw) if raw else None
    return account