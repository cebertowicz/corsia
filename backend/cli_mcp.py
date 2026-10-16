"""MCP configuration commands over the canonical connected-apps API."""

import argparse
import json
import sys
from typing import Any
from urllib.parse import quote


SERVERS_ROUTE = "/api/mcp/servers"


def _json_value(raw: str, expected: type, flag: str) -> Any:
    """Parse one JSON CLI argument without admitting a different wire shape."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"{flag} must be valid JSON: {error.msg}") from error
    if not isinstance(value, expected):
        raise argparse.ArgumentTypeError(f"{flag} must be a JSON {expected.__name__}")
    return value


def _json_object(raw: str) -> dict[str, Any]:
    return _json_value(raw, dict, "--config")


def _json_list(raw: str) -> list[dict[str, Any]]:
    value = _json_value(raw, list, "--key-refs")
    if not all(isinstance(item, dict) for item in value):
        raise argparse.ArgumentTypeError("--key-refs must be a JSON list of objects")
    return value


def _reach(raw: str) -> dict[str, Any]:
    return _json_value(raw, dict, "--reach")


def _server_path(server_id: str, suffix: str = "") -> str:
    return f"{SERVERS_ROUTE}/{quote(server_id, safe='')}{suffix}"


def _call_server_api(method: str, path: str, *, body: dict[str, Any] | None = None,
                     query: dict[str, str] | None = None) -> int:
    """Invoke the API as the CLI's actual token, leaving identity decisions to it."""
    from scripts import corsia_api

    response = corsia_api.request(
        method,
        corsia_api.resolve_base(None),
        path,
        corsia_api.resolve_token(None),
        body,
        query,
        header_auth=True,
    )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


def list_mcp_servers(args: argparse.Namespace) -> int:
    query = {"project": args.project} if args.project else (
        {"place": "workspace"} if args.workspace else None
    )
    return _call_server_api("GET", SERVERS_ROUTE, query=query)


def add_mcp_server(args: argparse.Namespace) -> int:
    body = {"name": args.name, "config": args.config, "key_refs": args.key_refs}
    if args.reach is not None:
        body["reach"] = args.reach
    return _call_server_api("POST", SERVERS_ROUTE, body=body)


def edit_mcp_server(args: argparse.Namespace) -> int:
    return _call_server_api(
        "PATCH",
        _server_path(args.server_id),
        body={
            "config": args.config,
            "key_refs": args.key_refs,
            "expected_revision": args.expected_revision,
        },
    )


def remove_mcp_server(args: argparse.Namespace) -> int:
    return _call_server_api("DELETE", _server_path(args.server_id))


def reconnect_mcp_server(args: argparse.Namespace) -> int:
    """Request reconnect; the server alone owns any live runtime effect."""
    return _call_server_api("POST", _server_path(args.server_id, "/reconnect"))


def approve_mcp_server(args: argparse.Namespace) -> int:
    return _call_server_api("POST", _server_path(args.server_id, "/approve"))


def configure_mcp(_args) -> int:
    """Refuse the retired settings-backed writer until it uses Connected apps."""
    print(
        "MCP configuration refused: Bringing apps over from other apps isn't available in this build yet; "
        "add the app in Connected apps.",
        file=sys.stderr,
    )
    return 2


def build_parser(subparsers) -> None:
    """Add MCP connected-app and legacy configuration commands to the maintenance CLI."""
    parser = subparsers.add_parser("mcp", help="Configure external MCP tools")
    commands = parser.add_subparsers(dest="mcp_command", required=True)

    list_servers = commands.add_parser("list", help="List connected MCP servers")
    place = list_servers.add_mutually_exclusive_group()
    place.add_argument("--project", help="List server reach for this project")
    place.add_argument("--workspace", action="store_true", help="List workspace-wide servers")
    list_servers.set_defaults(func=list_mcp_servers)

    add = commands.add_parser("add", help="Add a connected MCP server")
    add.add_argument("--name", required=True)
    add.add_argument("--config", required=True, type=_json_object, help="JSON config object")
    add.add_argument("--key-refs", required=True, type=_json_list, help="JSON list of key references")
    add.add_argument("--reach", type=_reach, help="JSON reach object; the server authorizes its use")
    add.set_defaults(func=add_mcp_server)

    edit = commands.add_parser("edit", help="Edit a connected MCP server")
    edit.add_argument("server_id")
    edit.add_argument("--config", required=True, type=_json_object, help="JSON config object")
    edit.add_argument("--key-refs", required=True, type=_json_list, help="JSON list of key references")
    edit.add_argument("--expected-revision", type=int, required=True)
    edit.set_defaults(func=edit_mcp_server)

    remove = commands.add_parser("remove", help="Remove a connected MCP server")
    remove.add_argument("server_id")
    remove.set_defaults(func=remove_mcp_server)

    reconnect = commands.add_parser("reconnect", help="Ask the server to reconnect an MCP app")
    reconnect.add_argument("server_id")
    reconnect.set_defaults(func=reconnect_mcp_server)

    approve = commands.add_parser("approve", help="Approve a pending MCP server change")
    approve.add_argument("server_id")
    approve.set_defaults(func=approve_mcp_server)

    configure = commands.add_parser("configure", help="Retired; add apps through Connected apps")
    configure.add_argument("file")
    configure.set_defaults(func=configure_mcp)
