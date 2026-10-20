#!/usr/bin/env python3
"""corsia delegate — delegated-thread CLI.

This CLI holds auth in a perms-scoped local credential so no bearer token enters
an agent's environment or prompt. It POSTs/GETs the same ``/api/delegation/*``
endpoints a remote/non-shell client would hit directly.

Subcommands:
    corsia delegate create-thread --model M [--title T] [--project-id P]
                                   [--provider PR] [--thread-kind chat|subagent]
                                   [--spawned-by PARENT | --from-thread-id SENDER]
    corsia delegate task <message...> --model M [--title T] [--provider PR]
    corsia delegate send <thread_id> <message...> [--no-sensitive-content]
    corsia delegate set-model <thread_id> <model> [--reason R]
    corsia delegate status <thread_id> [--messages N]

Auth (no env tokens): the API token is read from, in order —
    1. ``--token`` (explicit override).
    2. ``$CORSIA_API_TOKEN`` (operator/CI override).
    3. ``~/.corsia/.api-token`` — a perms-scoped (chmod 600) credential file
       containing just the token. The default agent path, provisioned once by
       the operator. When the file is absent/empty and no override is given,
       the resolver fails loud with a guided error (it never silently falls
       back to raw curl, and never materializes a token itself). Override the
       path with ``$CORSIA_API_TOKEN_FILE`` (the wrapper and tests use this).

Base URL: ``--base`` / ``$CORSIA_API_BASE`` / the backend's default loopback URL (see ``infra.default_port``).

No model literals here: the backend owns resolution. This CLI passes
``--model``/``--thread-kind`` through verbatim and prints what the backend
resolved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Shared auth + transport (one contract for delegate + doc CLIs — can't drift).
from corsia_api import DEFAULT_BASE as _DEFAULT_BASE
from corsia_api import request as _request
from corsia_api import resolve_base as _resolve_base
from corsia_api import resolve_token as _resolve_token


def cmd_create_thread(args: argparse.Namespace) -> int:
    """Create a delegated thread through the REST delegation API."""
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    body: dict = {}
    for key in ("title", "project_id", "model", "provider", "thread_kind", "spawned_by"):
        val = getattr(args, key)
        if val:
            body[key] = val
    if not body.get("spawned_by"):
        # The caller's own thread: a subagent's default parent, and for every
        # kind the agent whose Local only setting the new thread inherits.
        from_thread_id = _resolve_from_thread_id(args.from_thread_id)
        if from_thread_id:
            body["from_thread_id"] = from_thread_id
    out = _request("POST", base, "/api/delegation/threads", token, body=body)
    print(json.dumps(out, indent=2))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Send one message to a delegated thread."""
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    message = " ".join(args.message).strip()
    if not message:
        sys.stderr.write("Empty message.\n")
        return 2
    from_thread_id = _resolve_from_thread_id(args.from_thread_id)
    if not from_thread_id:
        # Fail loud locally — the /send endpoint requires verified sender
        # attribution (an un-attributed delegated send masquerades as a
        # user-authored message, bug #362). Surface the fix here rather than
        # let the request 400 server-side with less context.
        sys.stderr.write(
            "No sender thread id: a delegated /send must be attributed.\n"
            "  - run from a CORSIA thread context (CORSIA_THREAD_ID set), or\n"
            "  - pass --from-thread-id <your thread id>.\n"
        )
        return 2
    body: dict[str, object] = {"message": message, "from_thread_id": from_thread_id}
    if args.target_scope_id:
        body["target_scope_id"] = args.target_scope_id
    if args.no_sensitive_content:
        body["no_sensitive_content"] = True
    out = _request("POST", base, f"/api/delegation/threads/{args.thread_id}/send",
                   token, body=body)
    print(json.dumps(out, indent=2))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """Create a hidden child thread and send its first task turn."""
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    message = " ".join(args.message).strip()
    if not message:
        sys.stderr.write("Empty task.\n")
        return 2
    from_thread_id = _resolve_from_thread_id(args.from_thread_id)
    if not from_thread_id:
        sys.stderr.write(
            "No parent thread id: delegate task creates a hidden child.\n"
            "  - run from a CORSIA thread context (CORSIA_THREAD_ID set), or\n"
            "  - pass --from-thread-id <your thread id>.\n"
        )
        return 2
    create_body: dict = {
        "title": args.title or message[:60] or "Subagent",
        "thread_kind": "subagent",
        "from_thread_id": from_thread_id,
    }
    for key in ("model", "provider"):
        val = getattr(args, key)
        if val:
            create_body[key] = val
    created = _request("POST", base, "/api/delegation/threads", token, body=create_body)
    thread_id = created.get("thread_id")
    if not thread_id:
        sys.stderr.write("Create-thread response did not include thread_id.\n")
        print(json.dumps({"created": created}, indent=2))
        return 1
    sent = _request(
        "POST",
        base,
        f"/api/delegation/threads/{thread_id}/send",
        token,
        body={"message": message, "from_thread_id": from_thread_id},
    )
    print(json.dumps({"created": created, "sent": sent}, indent=2))
    return 0


def _resolve_from_thread_id(explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CORSIA_THREAD_ID")
    if env and env.strip():
        return env.strip()
    return None


def cmd_set_model(args: argparse.Namespace) -> int:
    """Switch a thread's selected model — the supported re-addressability repair.

    Recovers a thread wedged by the Codex resume guard (#447): the backend
    validates the explicit target is dispatchable (no silent default), records
    an audited model switch, and reports the continuity tradeoff. The
    pre-persist resume guard is never bypassed.
    """
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    from_thread_id = _resolve_from_thread_id(args.from_thread_id)
    if not from_thread_id:
        sys.stderr.write(
            "No sender thread id: a delegated set-model must be attributed.\n"
            "  - run from a CORSIA thread context (CORSIA_THREAD_ID set), or\n"
            "  - pass --from-thread-id <your thread id>.\n"
        )
        return 2
    body = {"model": args.model, "from_thread_id": from_thread_id}
    if args.reason:
        body["reason"] = args.reason
    out = _request(
        "POST", base,
        f"/api/delegation/threads/{args.thread_id}/set-model",
        token, body=body,
    )
    print(json.dumps(out, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print delegated-thread status and recent messages as JSON."""
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    out = _request("GET", base, f"/api/delegation/threads/{args.thread_id}/status",
                   token, query={"messages": str(args.messages)})
    print(json.dumps(out, indent=2))
    return 0


def _nonblank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("model must not be blank")
    return value.strip()


def _add_create_thread_parser(sub: argparse._SubParsersAction) -> None:
    ct = sub.add_parser("create-thread", help="Create a new delegated thread")
    ct.add_argument("--title")
    ct.add_argument("--project-id", dest="project_id")
    ct.add_argument("--model", required=True, type=_nonblank, help="Required explicit model id")
    ct.add_argument("--provider")
    ct.add_argument(
        "--thread-kind",
        choices=["chat", "subagent"],
        default="chat",
        help="Thread visibility kind: chat is visible; subagent is a hidden child and requires a parent",
    )
    ct.add_argument("--spawned-by", help="Parent thread id for a hidden subagent")
    ct.add_argument(
        "--from-thread-id",
        dest="from_thread_id",
        help="Your own thread id (default: $CORSIA_THREAD_ID): a subagent's parent, and the creator for any kind",
    )
    ct.set_defaults(func=cmd_create_thread)


def _add_task_parser(sub: argparse._SubParsersAction) -> None:
    task = sub.add_parser(
        "task",
        help="Create a hidden child thread and send its first task turn",
    )
    task.add_argument("message", nargs="+", help="The task text")
    task.add_argument("--title", help="Child thread title")
    task.add_argument("--model", required=True, type=_nonblank, help="Required explicit model id")
    task.add_argument("--provider")
    task.add_argument(
        "--from-thread-id",
        dest="from_thread_id",
        help="Parent/sender thread id (default: $CORSIA_THREAD_ID when present)",
    )
    task.set_defaults(func=cmd_task)


def _add_send_parser(sub: argparse._SubParsersAction) -> None:
    sd = sub.add_parser("send", help="Send a turn into a thread")
    sd.add_argument("thread_id")
    sd.add_argument("message", nargs="+", help="The message text")
    sd.add_argument(
        "--from-thread-id",
        dest="from_thread_id",
        help="Sender thread id (default: $CORSIA_THREAD_ID when present)",
    )
    sd.add_argument(
        "--target-scope-id",
        dest="target_scope_id",
        help=(
            "Required for intentional cross-scope sends; must match the target "
            "thread's project id, or 'workspace' for workspace threads"
        ),
    )
    sd.add_argument(
        "--no-sensitive-content",
        action="store_true",
        help="Confirm this cloud-bound send contains no sensitive content",
    )
    sd.set_defaults(func=cmd_send)


def _add_set_model_parser(sub: argparse._SubParsersAction) -> None:
    sm = sub.add_parser(
        "set-model",
        help="Switch a thread's selected model (resume-recovery for #447)",
    )
    sm.add_argument("thread_id")
    sm.add_argument("model", help="Explicit target dispatch form (e.g. "
                                  "claude-code/opus, anthropic/<id>, codex/...)")
    sm.add_argument("--reason", help="Audit reason (default: resume-recovery)")
    sm.add_argument(
        "--from-thread-id",
        dest="from_thread_id",
        help="Sender thread id (default: $CORSIA_THREAD_ID when present)",
    )
    sm.set_defaults(func=cmd_set_model)


def _add_status_parser(sub: argparse._SubParsersAction) -> None:
    st = sub.add_parser("status", help="Read a thread's turn-state + recent output")
    st.add_argument("thread_id")
    st.add_argument("--messages", type=int, default=5,
                    help="Recent messages to include (default 5, max 50)")
    st.set_defaults(func=cmd_status)


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch the requested delegate subcommand."""
    parser = argparse.ArgumentParser(
        prog="corsia delegate",
        description="Coordinator delegation — create threads, send turns, read status.",
    )
    parser.add_argument("--token", help="API token (default: ~/.corsia/.api-token)")
    parser.add_argument("--base", help=f"API base URL (default: {_DEFAULT_BASE})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    _add_create_thread_parser(sub)
    _add_task_parser(sub)
    _add_send_parser(sub)
    _add_set_model_parser(sub)
    _add_status_parser(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
