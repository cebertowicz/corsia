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


#: A hosted app's OAuth sign-in (``tools.mcp_oauth_store``): one entry per app holding the whole
#: grant, so a refresh rotates the access and refresh token in a single write.
OAUTH_PREFIX = "mcp-oauth-"
#: A grant carries tokens, client registration and endpoints, so it gets a higher ceiling than a key
#: (never split: a partial write would leave a half-signed-in server).
OAUTH_VALUE_MAX = 32 * 1024


def is_oauth_entry_name(name: str) -> bool:
    """Whether ``name`` is an OAuth sign-in entry."""
    return name.startswith(OAUTH_PREFIX) and is_mcp_entry_name(name)
