"""The Claude Code tool boundary must accept the MCP server key this instance registers.

`cc_event_tool_boundary` reports every MCP server whose name is not the one we register, so the
key is a pair: the config writer names it, the boundary recognizes it, and every reader of the
prefix follows the catalog. Deriving the key from the writer's own payload keeps the pair
honest -- renaming one side alone fails here, which is exactly the failure the check exists for
(a renamed key with an unrenamed boundary reports a violation on every Claude Code init).
"""

from __future__ import annotations

import json
from pathlib import Path

from subagents import cc_event_tool_boundary as boundary
from subagents.harness_tool_bridge import write_cc_mcp_config

# Test data: the legacy name, still the external-contract key.
LEGACY_NAME = "maestro"
# Test data: the new product name.
NEW_NAME = "corsia"


def _legacy_name() -> str:
    return LEGACY_NAME


def _registered_server_key() -> str:
    raw = write_cc_mcp_config(thread_id="thread-key-pairing", tool_token="token")
    path = Path(raw)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)
    keys = list(payload["mcpServers"])
    assert len(keys) == 1, f"expected one registered server, found {keys}"
    return keys[0]


def _init_event(server: str, *, with_tools: bool) -> dict:
    return {
        "mcp_servers": [{"name": server, "status": "ready"}],
        "tools": [f"mcp__{server}__read_file"] if with_tools else [],
        "skills": [],
        "slash_commands": [],
    }


def test_the_registered_server_key_is_not_a_boundary_violation() -> None:
    key = _registered_server_key()
    assert boundary._tool_boundary_violation_from_init(_init_event(key, with_tools=True)) is None


def test_a_server_under_another_key_is_a_boundary_violation() -> None:
    key = _registered_server_key()
    legacy = _legacy_name()
    assert key != legacy, "the registered key still reads as the legacy name"
    event = _init_event(legacy, with_tools=False)
    violation = boundary._tool_boundary_violation_from_init(event)
    assert violation is not None, "a server we do not register must be reported"