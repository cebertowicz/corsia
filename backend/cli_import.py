"""``corsia import <path>`` — bring an earlier CORSIA install's content over.

This follows ``corsia sources``: the shell mints a one-hour service token, this
command calls the running backend, and the running process does every write, so
the projects list and the shared index update as they do for any other change.

Without ``--apply`` it only says what would come over and changes nothing. No
secret is printed, and no file that failed a check is ever named with its
contents — only its path and the one word for why it stayed behind.
"""

from __future__ import annotations

import argparse
import json
import urllib.error

from cli_sources import RouteMissing, _read_token, request_json

ROUTE = "/api/import"
IMPORT_TIMEOUT_SECONDS = 600
NOT_SUPPORTED = "This version can't bring over an earlier install."
NO_TOKEN = "Couldn't get a credential for this. Start CORSIA, then try again."

_REASONS = {
    "secret": "a key or token in it",
    "screened": "never comes over",
    "database": "the earlier database",
    "link": "a link, not a file",
    "not_text": "not text",
    "too_large": "too large",
    "too_many": "more files than one import carries",
    "no_skill_md": "no SKILL.md",
    "no_description": "no description in its SKILL.md",
    "bad_name": "its name can't be a folder name",
}


def _reason(word: str) -> str:
    return _REASONS.get(word, word)


def _item(label: str, row: dict) -> str:
    state = str(row.get("state") or "")
    detail = str(row.get("detail") or "")
    mark = {"added": "+", "new": "·", "exists": "=", "skipped": "-"}.get(state, "!")
    return f"       {mark} {label}" + (f" — {detail}" if detail and state != "new" else "")


def _scope_lines(scope: dict) -> list[str]:
    """One line per context file, skill and shared document in one scope."""
    lines = []
    for row in scope.get("context") or []:
        lines.append(_item(str(row.get("path")), row))
    for row in scope.get("skills") or []:
        lines.append(_item(f"skill {row.get('name')}", row))
    for row in scope.get("shared") or []:
        lines.append(_item(str(row.get("path")), row))
    return lines


def format_report(report: dict) -> list[str]:
    """The terminal's whole account of one import: what came over and what did not."""
    applied = bool(report.get("applied"))
    lines = [
        f"Earlier install: {report.get('source')}",
        "Brought over:" if applied else "Would come over (nothing changed):",
    ]
    for project in report.get("projects") or []:
        state = str(project.get("state") or "")
        reason = str(project.get("reason") or "")
        head = f"     Project {project.get('name')}"
        lines.append(f"{head} — {reason}" if reason else head)
        lines.extend(_scope_lines(project))
    workspace = report.get("workspace") or {}
    if workspace.get("skills") or workspace.get("shared"):
        lines.append("     Workspace")
        lines.extend(_scope_lines(workspace))
    left = report.get("left_behind") or []
    if left:
        lines.append("     Left behind:")
        for row in left:
            lines.append(f"       - {row.get('where')}/{row.get('path')} — {_reason(str(row.get('reason')))}")
    if not applied:
        lines.append("Run it again with --apply to bring this over.")
    return lines


def cmd_import(args: argparse.Namespace) -> int:
    """Call the backend once and print its whole report."""
    token = _read_token()
    if not token:
        print(NO_TOKEN)
        return 2
    body = {"path": args.path, "apply": bool(args.apply)}
    try:
        report = request_json("POST", ROUTE, token, IMPORT_TIMEOUT_SECONDS, body=body)
    except RouteMissing:
        print(NOT_SUPPORTED)
        return 2
    except urllib.error.HTTPError as error:
        print(f"Couldn't bring it over: {_message(error)}")
        return 2
    except (OSError, ValueError) as error:
        print(f"Couldn't reach CORSIA: {error}")
        return 2
    print("\n".join(format_report(report)))
    return 0


def _message(error: urllib.error.HTTPError) -> str:
    """The backend's own sentence, or the status when it sent none."""
    try:
        detail = json.loads(error.read().decode() or "null")
    except (OSError, ValueError):
        detail = None
    if isinstance(detail, dict) and isinstance(detail.get("detail"), str):
        return detail["detail"]
    return f"HTTP {error.code}"


def build_parser(sub: argparse._SubParsersAction) -> None:
    """Register ``corsia import <path> [--apply]``."""
    command = sub.add_parser(
        "import", help="Bring projects, skills and shared documents over from an earlier CORSIA",
    )
    command.add_argument("path", help="Full path to the earlier CORSIA workspace folder")
    command.add_argument(
        "--apply", action="store_true", help="Make the changes (without this, nothing is written)",
    )
    command.set_defaults(func=cmd_import)
