#!/usr/bin/env python3
"""corsia context — approved CTX bootloader management commands."""

from __future__ import annotations

import argparse
import json

from corsia_api import DEFAULT_BASE as _DEFAULT_BASE
from corsia_api import request as _request
from corsia_api import resolve_base as _resolve_base
from corsia_api import resolve_token as _resolve_token


def _target_body(args: argparse.Namespace) -> dict:
    body = {"scope": args.scope}
    if args.scope == "project":
        body["project_id"] = args.target_id
    elif args.scope == "thread":
        body["thread_id"] = args.target_id
    return body


def cmd_attach(args: argparse.Namespace) -> int:
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    body = {**_target_body(args), "path": args.path}
    out = _request("POST", base, "/api/context/bootloader/attach", token, body=body)
    print(json.dumps(out, indent=2))
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    body = {
        **_target_body(args),
        "path": args.path,
        "content": args.content or f"# {args.path}\n",
    }
    out = _request("POST", base, "/api/context/bootloader/file", token, body=body)
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corsia context",
        description="Manage workspace, project, and thread CTX bootloader files.",
    )
    parser.add_argument("--token", help="API token (default: ~/.corsia/credentials/api-token)")
    parser.add_argument("--base", help=f"API base URL (default: {_DEFAULT_BASE})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    attach = sub.add_parser("attach", help="Attach an existing file to a bootloader")
    attach.add_argument("scope", choices=["workspace", "global", "project", "thread"])
    attach.add_argument("target_id", help="project id or thread id; ignored for workspace/global")
    attach.add_argument("path")
    attach.set_defaults(func=cmd_attach)

    create = sub.add_parser("create", help="Create and attach a bootloader file")
    create.add_argument("scope", choices=["workspace", "global", "project", "thread"])
    create.add_argument("target_id", help="project id or thread id; ignored for workspace/global")
    create.add_argument("path")
    create.add_argument("--content")
    create.set_defaults(func=cmd_create)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
