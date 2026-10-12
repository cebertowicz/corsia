"""The key store names MCP credentials use.

An MCP server's credential env value is the entry
``mcp-<server slug>-<variable slug>-<10 hex digits of a hash>``, derived by
``tools.mcp_secrets.entry_name``. The prefix and shape live here, apart from
``key_store``, so its name rules can ask whether a name is one of these without
importing the tools package.
"""

from __future__ import annotations

import re

PREFIX = "mcp-"
#: What follows the prefix: a server slug, a variable slug, then the hash.
_ENTRY_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,23}[a-z0-9])?-[0-9a-f]{10}$")


def is_mcp_entry_name(name: str) -> bool:
    """Whether ``name`` has the shape of an MCP credential entry, not just the prefix."""
    return name.startswith(PREFIX) and bool(_ENTRY_PATTERN.match(name[len(PREFIX):]))
