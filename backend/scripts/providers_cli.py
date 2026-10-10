#!/usr/bin/env python3
"""corsia providers — CLI shim over the provider and model-catalog APIs.

Subcommands:
    corsia providers list                          providers and endpoints, with status
    corsia providers models [--provider P] [--all] catalog rows (--all adds hidden ones)
    corsia providers add --name N --base-url URL [--dialect D] [--model ID ...]
                          [--key-stdin | --key-env VAR]
    corsia providers edit <endpoint> [--base-url URL] [--dialect D]
                          [--model ID ... | --clear-models] [--key-stdin | --key-env VAR]
    corsia providers remove <endpoint>
    corsia providers test <endpoint>              probe now and rediscover its models
    corsia providers enable|disable <endpoint>
    corsia providers show|hide <model>            put a model in or out of the picker
    corsia providers rename <model> <label>       an empty label restores the discovered name
    corsia providers window <model> <tokens|clear>

``<endpoint>`` is an External endpoint's id or its exact name. The credential is
read from ``$CORSIA_API_TOKEN`` or the token file and sent only as a ``Bearer``
header. An endpoint's API key is read from stdin or a named environment variable,
never from an argument, and no key is ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote

from corsia_api import request as _request
from corsia_api import resolve_base as _resolve_base
from corsia_api import resolve_token as _resolve_token

_SERVERS = "/api/settings/network-servers"
_MODELS = "/api/settings/models"


class _Api:
    def __init__(self, base: str, cred: str):
        self.base, self.cred = base, cred

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        return _request(method, self.base, path, self.cred, body=body, header_auth=True)


def _fail(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def _model_path(model_id: str) -> str:
    return quote(model_id, safe="/")


def _endpoints(api: _Api) -> list[dict]:
    return [s for s in api("GET", _SERVERS).get("servers", []) if s.get("status") != "removed"]


def _endpoint(api: _Api, ref: str) -> dict:
    """Resolve an endpoint by id, then by exact (case-insensitive) name."""
    rows = _endpoints(api)
    for row in rows:
        if row.get("id") == ref:
            return row
    named = [r for r in rows if str(r.get("instance_name", "")).lower() == ref.lower()]
    if len(named) == 1:
        return named[0]
    known = ", ".join(f"{r.get('instance_name')} ({r.get('id')})" for r in rows) or "none"
    reason = "is ambiguous" if named else "was not found"
    _fail(f"Endpoint '{ref}' {reason}. Known endpoints: {known}")
    raise AssertionError("unreachable")


def _read_key(args) -> str | None:
    if getattr(args, "key_env", None):
        value = os.environ.get(args.key_env, "").strip()
        if not value:
            _fail(f"${args.key_env} is empty or unset")
        return value
    if getattr(args, "key_stdin", False):
        value = sys.stdin.readline().strip()
        if not value:
            _fail("No key on stdin")
        return value
    return None


def _endpoint_view(row: dict) -> dict:
    meta = row.get("metadata") or {}
    discovery = meta.get("discovery") or {}
    return {
        "id": row.get("id"),
        "name": row.get("instance_name"),
        "provider": row.get("provider"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "dialect": row.get("dialect"),
        "base_url": meta.get("base_url") or row.get("api_base"),
        "has_key": bool(row.get("has_key")),
        "declared_models": meta.get("declared_models") or [],
        "discovery_error": discovery.get("error"),
    }


def _wire_label(endpoint: dict) -> str:
    """An External endpoint's dialect; any other server's kind."""
    if endpoint["kind"] == "external":
        return str(endpoint["dialect"] or "")
    return str(endpoint["kind"] or "")


def _provider_view(row: dict) -> dict:
    return {k: row.get(k) for k in ("name", "label", "type", "has_key", "enabled", "unsupported")}


def _model_view(row: dict) -> dict:
    keys = ("id", "provider", "harness", "litellm_model", "display_name", "alias", "enabled",
            "available", "context_window", "last_error")
    return {k: row.get(k) for k in keys if k in row}


def _label(model: dict) -> str:
    """The name the picker shows: a saved label, else the discovered name."""
    if model.get("alias"):
        return str(model["alias"])
    return str(model.get("display_name", ""))


def _emit(args, data, lines) -> None:
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return
    for line in lines:
        print(line)


def cmd_list(api: _Api, args) -> None:
    """List built-in providers and endpoints with status and any discovery error."""
    providers = [_provider_view(p) for p in api("GET", "/api/settings/providers").get("providers", [])]
    endpoints = [_endpoint_view(s) for s in _endpoints(api)]
    lines = ["Providers:"] + [
        f"  {p['name']:<14} {p.get('type') or '':<8} key={'yes' if p.get('has_key') else 'no'}"
        for p in providers
    ] + ["Endpoints:"] + [
        f"  {e['id']:<18} {e['name'] or '':<22} {e['status'] or '':<9} {_wire_label(e)}"
        + (f"  error: {e['discovery_error']}" if e["discovery_error"] else "")
        for e in endpoints
    ]
    _emit(args, {"providers": providers, "endpoints": endpoints}, lines)


def cmd_models(api: _Api, args) -> None:
    """List catalog rows, shown ones only unless --all."""
    rows = [_model_view(m) for m in api("GET", _MODELS).get("models", [])]
    if args.provider:
        # An External row's selector is external@<endpoint id>/<model>.
        rows = [m for m in rows if args.provider in (m.get("provider"), m.get("harness"))
                or str(m.get("litellm_model", "")).startswith(f"external@{args.provider}/")]
    if not args.all:
        rows = [m for m in rows if m.get("enabled")]
    lines = [
        f"  {m['id']:<30} {_label(m):<28}"
        f" {'shown' if m.get('enabled') else 'hidden':<7} {'available' if m.get('available') else 'unavailable'}"
        for m in rows
    ] or ["  (no models)"]
    _emit(args, {"models": rows}, lines)


def cmd_add(api: _Api, args) -> None:
    """Register an External endpoint; the server probes it before saving."""
    body: dict = {"name": args.name, "base_url": args.base_url, "dialect": args.dialect}
    if args.model:
        body["model_ids"] = args.model
    key = _read_key(args)
    if key:
        body["api_key"] = key
    result = api("POST", _SERVERS, body)
    _emit(args, result, [f"Added {result.get('server_id')}: {result.get('models', '?')} models"])


def cmd_edit(api: _Api, args) -> None:
    """Change an External endpoint, sending back the stored URL and dialect it keeps."""
    row = _endpoint_view(_endpoint(api, args.endpoint))
    if row["kind"] != "external":
        _fail(f"{row['id']} is not an External endpoint; only those are edited here")
    body: dict = {
        "base_url": args.base_url or row["base_url"],
        "dialect": args.dialect or row["dialect"] or "openai",
    }
    if args.clear_models:
        body["model_ids"] = []
    elif args.model:
        body["model_ids"] = args.model
    key = _read_key(args)
    if key:
        body["api_key"] = key
    result = api("PUT", f"{_SERVERS}/{row['id']}", body)
    _emit(args, result, [f"Updated {row['id']}: {result.get('models', '?')} models"])


def cmd_remove(api: _Api, args) -> None:
    """Remove an endpoint and its models."""
    row = _endpoint(api, args.endpoint)
    result = api("DELETE", f"{_SERVERS}/{row['id']}")
    _emit(args, result, [f"Removed {row['id']}"])


def cmd_test(api: _Api, args) -> None:
    """Probe an endpoint now; exit 1 with the cause when it fails."""
    row = _endpoint(api, args.endpoint)
    result = api("POST", f"{_SERVERS}/{row['id']}/test")
    _emit(args, result, [f"{'OK' if result.get('ok') else 'FAILED'}: {result.get('message', '')}"])
    if not result.get("ok"):
        sys.exit(1)


def cmd_enabled(api: _Api, args) -> None:
    """Switch an endpoint on or off."""
    row = _endpoint(api, args.endpoint)
    on = args.cmd == "enable"
    result = api("PUT", f"{_SERVERS}/{row['id']}/enabled", {"enabled": on})
    _emit(args, result, [f"{'Enabled' if on else 'Disabled'} {row['id']}"])


def cmd_visibility(api: _Api, args) -> None:
    """Put a model in or take it out of the picker."""
    shown = args.cmd == "show"
    result = api("PATCH", f"{_MODELS}/{_model_path(args.model)}", {"enabled": shown})
    _emit(args, result, [f"{'Shown' if shown else 'Hidden'}: {args.model}"])


def cmd_rename(api: _Api, args) -> None:
    """Set or clear a model's display label."""
    result = api("PATCH", f"{_MODELS}/{_model_path(args.model)}", {"alias": args.label})
    _emit(args, result, [f"Renamed {args.model}" if args.label.strip() else f"Label cleared: {args.model}"])


def cmd_window(api: _Api, args) -> None:
    """Set a model's context window, or clear it back to the discovered one."""
    if args.tokens.lower() == "clear":
        window = None
    else:
        try:
            window = int(args.tokens.replace("_", "").replace(",", ""))
        except ValueError:
            _fail("window takes a positive whole number of tokens, or 'clear'")
    result = api("PATCH", f"/api/models/{_model_path(args.model)}/profile", {"context_window": window})
    shown = "cleared (the discovered window applies)" if window is None else f"set to {window:,}"
    _emit(args, result, [f"Context window for {args.model} {shown}"])


def _add_key_options(p: argparse.ArgumentParser) -> None:
    group = p.add_mutually_exclusive_group()
    group.add_argument("--key-stdin", action="store_true", help="read the endpoint's API key from stdin")
    group.add_argument("--key-env", metavar="VAR", help="read the endpoint's API key from $VAR")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser; no option accepts a credential."""
    parser = argparse.ArgumentParser(prog="corsia providers", description=__doc__.split("\n")[0])
    parser.add_argument("--base", help="API base URL (default $CORSIA_API_BASE or local)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="providers and endpoints, with status")
    p = sub.add_parser("models", help="catalog rows")
    p.add_argument("--provider", help="filter by provider, harness or endpoint id")
    p.add_argument("--all", action="store_true", help="include hidden models")

    p = sub.add_parser("add", help="register an External endpoint")
    p.add_argument("--name", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--dialect", choices=("openai", "anthropic"), default="openai")
    p.add_argument("--model", action="append", help="a model id it serves, if it cannot list them (repeatable)")
    _add_key_options(p)

    p = sub.add_parser("edit", help="change an External endpoint (omitted fields are kept)")
    p.add_argument("endpoint")
    p.add_argument("--base-url")
    p.add_argument("--dialect", choices=("openai", "anthropic"))
    models = p.add_mutually_exclusive_group()
    models.add_argument("--model", action="append", help="replace the declared model ids (repeatable)")
    models.add_argument("--clear-models", action="store_true", help="drop the declared model ids")
    _add_key_options(p)

    for name, text in (("remove", "remove an endpoint"), ("test", "probe an endpoint now"),
                       ("enable", "switch an endpoint on"), ("disable", "switch an endpoint off")):
        sub.add_parser(name, help=text).add_argument("endpoint")
    for name, text in (("show", "put a model in the picker"), ("hide", "take a model out of the picker")):
        sub.add_parser(name, help=text).add_argument("model")
    p = sub.add_parser("rename", help="set a model's display label")
    p.add_argument("model")
    p.add_argument("label")
    p = sub.add_parser("window", help="set a model's context window")
    p.add_argument("model")
    p.add_argument("tokens", help="a whole number of tokens, or 'clear'")
    return parser


_COMMANDS = {
    "list": cmd_list, "models": cmd_models, "add": cmd_add, "edit": cmd_edit,
    "remove": cmd_remove, "test": cmd_test, "enable": cmd_enabled, "disable": cmd_enabled,
    "show": cmd_visibility, "hide": cmd_visibility, "rename": cmd_rename, "window": cmd_window,
}


def main(argv: list[str] | None = None) -> None:
    """Run one subcommand."""
    args = build_parser().parse_args(argv)
    api = _Api(_resolve_base(args.base), _resolve_token(None))
    _COMMANDS[args.cmd](api, args)


if __name__ == "__main__":
    main()
