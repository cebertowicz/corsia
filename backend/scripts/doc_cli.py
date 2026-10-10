#!/usr/bin/env python3
"""corsia doc — CLI shim over document-comment APIs.

Subcommands:
    corsia doc comment <path> --quote "<span>" [--near "<hint>"] "<body>"
    corsia doc review  <path> "<body>"
    corsia doc request-review <path> --reviewer <thread_id> [--note ...] [--client-id ...]
    corsia doc review-cancel <request_id>
    corsia doc reply   <comment_id> "<body>"
    corsia doc resolve <comment_id>
    corsia doc ack     <comment_id>
    corsia doc submit  <path>
    corsia doc retry   <path>
    corsia doc accept|reject <comment_id> [--all-from-author]
    corsia doc comments <path> [--unresolved] [--json]      (the READ side)

``<body>`` is a single shell argument. ``--as`` or ``$CORSIA_THREAD_ID`` is
required so comments carry an authoring thread.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from corsia_api import DEFAULT_BASE as _DEFAULT_BASE
from corsia_api import request as _request
from corsia_api import resolve_base as _resolve_base
from corsia_api import resolve_token as _resolve_token

_COMMENTS_PATH = "/api/delegation/docs/comments"
_FEEDBACK_PATH = "/api/delegation/docs/feedback"


def _abspath(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _resolve_as_thread(explicit: str | None) -> str:
    """Resolve authoring thread from --as or $CORSIA_THREAD_ID."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CORSIA_THREAD_ID")
    if env and env.strip():
        return env.strip()
    sys.stderr.write(
        "No authoring thread.\n"
        "  Pass --as <thread_id>, or set $CORSIA_THREAD_ID.\n"
        "  A CORSIA-spawned agent gets $CORSIA_THREAD_ID in its env "
        "automatically; outside one, pass --as.\n"
    )
    sys.exit(2)


def _target_fields(target: str, is_id: bool) -> dict:
    """Return the doc_path or artifact_id body field."""
    return {"artifact_id": target} if is_id else {"doc_path": _abspath(target)}


def _create(args: argparse.Namespace, extra: dict) -> dict:
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    body = (args.body or "").strip()
    if not body and "replacement" not in extra:
        sys.stderr.write("Empty body.\n")
        sys.exit(2)
    payload = {"body": body, "as_session_id": _resolve_as_thread(args.as_thread), **extra}
    return _request("POST", base, _COMMENTS_PATH, token, body=payload)


def cmd_comment(args: argparse.Namespace) -> int:
    extra = _target_fields(args.target, args.is_id)
    extra["quote"] = args.quote
    if args.near:
        extra["near"] = args.near
    print(json.dumps(_create(args, extra), indent=2))
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    extra = _target_fields(args.target, args.is_id)
    extra.update({"quote": args.quote, "replacement": args.replacement})
    if args.near:
        extra["near"] = args.near
    print(json.dumps(_create(args, extra), indent=2))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    extra = _target_fields(args.target, args.is_id)
    extra["review"] = True
    print(json.dumps(_create(args, extra), indent=2))
    return 0


def cmd_request_review(args: argparse.Namespace) -> int:
    payload = {
        **_target_fields(args.target, args.is_id),
        "reviewer": args.reviewer, "note": args.note,
        "client_id": args.client_id or str(uuid.uuid4()),
        "as_session_id": _resolve_as_thread(args.as_thread),
    }
    result = _request("POST", _resolve_base(args.base), "/api/delegation/docs/reviews",
                      _resolve_token(args.token), body=payload)
    print(json.dumps(result, indent=2))
    return 0


def cmd_review_cancel(args: argparse.Namespace) -> int:
    result = _request("DELETE", _resolve_base(args.base),
                      f"/api/delegation/docs/reviews/{args.request_id}",
                      _resolve_token(args.token),
                      query={"as_session_id": _resolve_as_thread(args.as_thread)})
    print(json.dumps(result, indent=2))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    extra: dict = {"parent_id": args.comment_id}
    if args.id:
        extra["artifact_id"] = args.id
    elif args.path:
        extra["doc_path"] = _abspath(args.path)
    print(json.dumps(_create(args, extra), indent=2))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """Submit or retry this agent's batches on one registered document."""
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    payload = {
        **_target_fields(args.target, args.is_id),
        "as_session_id": _resolve_as_thread(args.as_thread),
    }
    result = _request(
        "POST", base, f"{_FEEDBACK_PATH}/{args.action}", token, body=payload,
    )
    print(json.dumps(result, indent=2))
    return 0


def _patch(args: argparse.Namespace, fields: dict) -> dict:
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    payload = {**fields, "as_session_id": _resolve_as_thread(args.as_thread)}
    return _request("PATCH", base, f"{_COMMENTS_PATH}/{args.comment_id}", token, body=payload)


def cmd_resolve(args: argparse.Namespace) -> int:
    print(json.dumps(_patch(args, {"resolved": True}), indent=2))
    return 0


def cmd_decision(args: argparse.Namespace) -> int:
    print(json.dumps(_patch(args, {
        "decision": args.decision,
        "all_from_author": args.all_from_author,
    }), indent=2))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    print(json.dumps(_patch(args, {"acked": True}), indent=2))
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    """Resolve the path to an artifact id, then list comment threads."""
    token = _resolve_token(args.token)
    base = _resolve_base(args.base)
    canonical = _abspath(args.path)
    resolved = _request(
        "GET",
        base,
        "/api/artifacts/resolve",
        token,
        query={"path": canonical},
    )
    art = resolved.get("artifact")
    if not art:
        sys.stderr.write(f"No registered doc artifact for {canonical} (no comments yet).\n")
        return 1
    out = _request("GET", base, f"/api/artifacts/{art['id']}/comments", token)
    threads = out.get("comments", [])
    if args.unresolved:
        threads = [t for t in threads if not t.get("resolved")]
    if args.json:
        print(json.dumps(threads, indent=2))
    else:
        _print_threads(threads)
    return 0


def _print_threads(threads: list) -> None:
    if not threads:
        print("(no comments)")
        return
    for t in threads:
        flag = "✓" if t.get("resolved") else "•"
        acked = " (acked)" if t.get("acked") and not t.get("resolved") else ""
        anchor = (f" [{t['anchor_text']}]" if t.get("anchor_text")
                  else (" [review]" if t.get("review") else ""))
        print(f"{flag} {t.get('id')} {t.get('author')} ({t.get('author_kind')}){anchor}{acked}")
        print(f"    {t.get('body', '').strip()}")
        for r in t.get("replies", []):
            print(f"    ↳ {r.get('author')}: {r.get('body', '').strip()}")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as", dest="as_thread",
                   help="Authoring thread id (default: $CORSIA_THREAD_ID)")


def _add_review_request_commands(sub) -> None:
    rq = sub.add_parser("request-review", help="Queue a review task in another agent's plan")
    rq.add_argument("target", help="Doc path (or an artifact id with --by-id)")
    rq.add_argument("--reviewer", required=True, help="Reviewer thread ID from agent_roster")
    rq.add_argument("--note", help="Optional requester's note (at most 500 characters)")
    rq.add_argument("--client-id", help="Stable request key for retries")
    rq.add_argument("--by-id", dest="is_id", action="store_true")
    _add_common(rq)
    rq.set_defaults(func=cmd_request_review)

    rc = sub.add_parser("review-cancel", help="Cancel a document review request")
    rc.add_argument("request_id")
    _add_common(rc)
    rc.set_defaults(func=cmd_review_cancel)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corsia doc",
        description="Agent doc-collab — comment / reply / review / resolve / ack / read.")
    parser.add_argument("--token", help="API token (default: ~/.corsia/.api-token)")
    parser.add_argument("--base", help=f"API base URL (default: {_DEFAULT_BASE})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("comment", help="Anchored comment on a doc span")
    c.add_argument("target", help="Doc path (or an artifact id with --id)")
    c.add_argument("body", help='Comment body — one arg, e.g. "$(cat msg.md)"')
    c.add_argument("--quote", required=True, help="The span text to anchor to")
    c.add_argument("--near", help="Disambiguating hint for a repeated span")
    c.add_argument("--by-id", dest="is_id", action="store_true", help="Treat <target> as an artifact id, not a path")
    _add_common(c)
    c.set_defaults(func=cmd_comment)

    sg = sub.add_parser("suggest", help="Suggest an anchored replacement")
    sg.add_argument("target", help="Doc path (or an artifact id with --id)")
    sg.add_argument("quote", help="Exact current text to replace")
    sg.add_argument("replacement", help="Replacement text; empty deletes the quote")
    sg.add_argument("body", nargs="?", default="", help="Optional note")
    sg.add_argument("--near", help="Disambiguating hint for a repeated span")
    sg.add_argument("--by-id", dest="is_id", action="store_true")
    _add_common(sg)
    sg.set_defaults(func=cmd_suggest)

    rv = sub.add_parser("review", help="Whole-doc review comment")
    rv.add_argument("target", help="Doc path (or an artifact id with --id)")
    rv.add_argument("body", help="Review body — one arg")
    rv.add_argument("--by-id", dest="is_id", action="store_true", help="Treat <target> as an artifact id, not a path")
    _add_common(rv)
    rv.set_defaults(func=cmd_review)

    _add_review_request_commands(sub)

    rp = sub.add_parser("reply", help="Reply to a comment thread")
    rp.add_argument("comment_id", help="The parent comment id")
    rp.add_argument("body", help="Reply body — one arg")
    rp.add_argument("--path", help="Doc path (optional; scopes the reply)")
    rp.add_argument("--id", help="Artifact id (optional; scopes the reply)")
    _add_common(rp)
    rp.set_defaults(func=cmd_reply)

    for name in ("submit", "retry"):
        fb = sub.add_parser(name, help=f"{name.title()} feedback batches on a document")
        fb.add_argument("target", help="Doc path (or an artifact id with --id)")
        fb.add_argument("--by-id", dest="is_id", action="store_true")
        _add_common(fb)
        fb.set_defaults(func=cmd_feedback, action=name)

    rs = sub.add_parser("resolve", help="Resolve (close) a comment thread")
    rs.add_argument("comment_id")
    _add_common(rs)
    rs.set_defaults(func=cmd_resolve)

    for name, decision in (("accept", "accept"), ("reject", "reject")):
        dp = sub.add_parser(name, help=f"{name.title()} a document suggestion")
        dp.add_argument("comment_id")
        dp.add_argument("--all-from-author", action="store_true")
        _add_common(dp)
        dp.set_defaults(func=cmd_decision, decision=decision)

    ak = sub.add_parser("ack", help="Acknowledge a comment (agree; ends the exchange)")
    ak.add_argument("comment_id")
    _add_common(ak)
    ak.set_defaults(func=cmd_ack)

    cm = sub.add_parser("comments", help="List a doc's comment threads (read)")
    cm.add_argument("path", help="Doc path")
    cm.add_argument("--unresolved", action="store_true", help="Only unresolved threads")
    cm.add_argument("--json", action="store_true", help="Raw JSON output")
    _add_common(cm)
    cm.set_defaults(func=cmd_comments)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
