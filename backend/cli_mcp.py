"""Operator configuration of external tools through the canonical settings owner."""

import json
from pathlib import Path
import sys

from tools import mcp_secrets
from tools.mcp_config import MCPConfigError, allowed_names, http_options, timeout_seconds


def configure_mcp(args) -> int:
    """Validate and replace MCP server configuration.

    Credential-named env and header values go to the key store; the saved
    config keeps references. Bearer values are never persisted.
    """
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    try:
        servers = json.loads(Path(args.file).expanduser().read_text())
        if not isinstance(servers, dict):
            raise MCPConfigError("configuration must be an object keyed by server name")
        for name, config in servers.items():
            if not isinstance(config, dict):
                raise MCPConfigError("each server configuration must be an object")
            config, _ = mcp_secrets.split_secrets(name, config)
            allowed_names(config)
            timeout_seconds(config, "connect_timeout", 10)
            timeout_seconds(config, "tool_timeout", 30)
            if config.get("url"):
                http_options(config)
            elif not config.get("command") or config.get("bearer_file"):
                raise MCPConfigError("configure an HTTP URL or a stdio command without bearer_file")
        with write_transaction(None, scope="configure_mcp") as db:
            tools = get_setting("tools", db=db) or {}
            servers = mcp_secrets.replace_servers(tools.get("mcpServers") or {}, servers, db=db)
            tools["mcpServers"] = servers
            set_setting("tools", tools, db=db)
    except MCPConfigError as error:
        print(f"MCP configuration refused: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print("MCP configuration refused: cannot read a valid JSON configuration file", file=sys.stderr)
        return 2
    print(f"Configured {len(servers)} MCP servers. Restart this backend to discover their tools.")
    return 0


def build_parser(subparsers) -> None:
    """Add the MCP configuration command to CORSIA's maintenance CLI."""
    parser = subparsers.add_parser("mcp", help="Configure external MCP tools")
    commands = parser.add_subparsers(dest="mcp_command", required=True)
    configure = commands.add_parser("configure", help="Replace tools.mcpServers from a JSON file")
    configure.add_argument("file")
    configure.set_defaults(func=configure_mcp)
