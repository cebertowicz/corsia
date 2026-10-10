#!/usr/bin/env python3
"""Guard a Gmail reply before it reaches ``gws gmail +reply``.

Exit status: 0 preview/sent, 1 malformed input or gws error, 2 warning not
acknowledged, 3 a blocking guard.  This program intentionally never accepts a
recipient: the reply engine derives it from the message being answered.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from email.utils import parseaddr
from pathlib import Path
from typing import Any, NoReturn

NO_REPLY = re.compile(r"(^|[.@_-])(?:no[-_]?reply|do[-_]?not[-_]?reply|donotreply)([.@_-]|$)", re.I)
SUBJECT_PREFIX = re.compile(r"^\s*(?:re|fwd|fw|aw|sv|antw|wg)\s*:\s*", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-id", required=True)
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-stale-ok", action="store_true")
    parser.add_argument("--pivot-to")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def gws(*args: str) -> dict[str, Any]:
    result = subprocess.run(["gws", *args], capture_output=True, text=True, check=False)
    output = "\n".join(line for line in result.stdout.splitlines() if not line.startswith("Using keyring"))
    if result.returncode:
        message = result.stderr.strip() or output.strip() or "gws command failed"
        raise RuntimeError(message)
    if not output.strip():
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("gws returned non-JSON output") from error


def headers(message: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in message.get("payload", {}).get("headers", [])
    }


def sender(message: dict[str, Any]) -> str:
    return parseaddr(headers(message).get("from", ""))[1].lower()


def recipient(message: dict[str, Any]) -> str:
    values = headers(message)
    return parseaddr(values.get("reply-to") or values.get("from", ""))[1].lower()


def timestamp(message: dict[str, Any]) -> int:
    try:
        return int(message.get("internalDate", 0))
    except (TypeError, ValueError):
        return 0


def normalized_subject(message: dict[str, Any]) -> str:
    value = headers(message).get("subject", "")
    while SUBJECT_PREFIX.match(value):
        value = SUBJECT_PREFIX.sub("", value, count=1)
    return " ".join(value.lower().split())


def messages(thread: dict[str, Any]) -> list[dict[str, Any]]:
    return list(thread.get("messages") or [])


def stop(code: int, message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def body_text(args: argparse.Namespace) -> str:
    if args.body_file is None:
        return args.body or ""
    try:
        return args.body_file.read_text(encoding="utf-8")
    except OSError:
        stop(1, "ERROR: Reply body file could not be read.")


def target_for(args: argparse.Namespace) -> str:
    return args.pivot_to or args.message_id


def guard(args: argparse.Namespace, target: dict[str, Any], text: str) -> None:
    # G2 and G3: never accept a model-provided recipient or empty content.
    reply_to = recipient(target)
    if not reply_to:
        stop(3, "FAIL G2: target message has no reply recipient; nothing sent.")
    if NO_REPLY.search(reply_to):
        stop(3, "FAIL G2: target is a no-reply address; nothing sent.")
    if not text.strip():
        stop(3, "FAIL G3: reply body is empty; nothing sent.")

    # G4 and G5: get a canonical in-thread order and discover the signed-in self.
    thread = gws("gmail", "users", "threads", "get", "--params", json.dumps({"userId": "me", "id": target["threadId"]}))
    profile = gws("gmail", "users", "getProfile", "--params", json.dumps({"userId": "me"}))
    self_address = str(profile.get("emailAddress", "")).lower()
    newer = [item for item in messages(thread) if timestamp(item) > timestamp(target)]
    if any(sender(item) == self_address for item in newer):
        stop(3, "FAIL G4: a reply already exists after the target; nothing sent.")
    external_newer = [item for item in newer if sender(item) != self_address]
    same_sender = [item for item in external_newer if sender(item) == sender(target)]
    if same_sender and not (args.confirm_stale_ok or args.force):
        stop(3, "FAIL G5: the sender moved this thread forward; use --pivot-to or --confirm-stale-ok.")
    if external_newer and not (args.confirm_stale_ok or args.force):
        stop(2, "WARN G5: newer messages exist in this thread; use --confirm-stale-ok after reading them.")

    # G6: Gmail list responses only carry ids and thread ids. Hydrate each
    # result before comparing its real internalDate and normalized subject.
    subject = normalized_subject(target)
    if subject:
        query = f'from:{sender(target)} subject:"{subject}"'
        found = gws("gmail", "users", "messages", "list", "--params", json.dumps({"userId": "me", "q": query, "maxResults": 25}))
        parallel = []
        for summary in found.get("messages", []):
            message_id = summary.get("id")
            if not message_id or message_id == target.get("id"):
                continue
            if summary.get("threadId") == target.get("threadId"):
                continue
            candidate = gws(
                "gmail", "users", "messages", "get", "--params",
                json.dumps({
                    "userId": "me", "id": message_id, "format": "metadata",
                    "metadataHeaders": ["From", "Subject"],
                }),
            )
            if candidate.get("threadId") == target.get("threadId"):
                continue
            if normalized_subject(candidate) != subject:
                continue
            if timestamp(candidate) > timestamp(target):
                parallel.append(candidate)
        if parallel and not (args.confirm_stale_ok or args.force):
            stop(3, "FAIL G6: a newer same-subject message exists in another thread; use --pivot-to or --confirm-stale-ok.")


def invoke_reply(args: argparse.Namespace, target_id: str, text: str, send: bool) -> None:
    command = ["gmail", "+reply", "--message-id", target_id, "--body", text]
    for attachment in args.attach:
        command.extend(["--attach", attachment])
    if not send:
        command.append("--dry-run")
    gws(*command)


def main() -> None:
    args = parse_args()
    target_id = target_for(args)
    text = body_text(args)
    try:
        # G1: the target must be readable before every remaining check.
        target = gws("gmail", "users", "messages", "get", "--params", json.dumps({"userId": "me", "id": target_id, "format": "full"}))
        if not target.get("id"):
            stop(3, "FAIL G1: target message is not readable; nothing sent.")
        guard(args, target, text)
        # G8: preview is always run first, even for an explicit send request.
        invoke_reply(args, target_id, text, send=False)
        if not args.send or args.dry_run:
            print("Preview complete — nothing sent.")
            return
        invoke_reply(args, target_id, text, send=True)
        print("Reply sent after guard preview.")
    except RuntimeError as error:
        stop(1, f"ERROR: {error}")


if __name__ == "__main__":
    main()
