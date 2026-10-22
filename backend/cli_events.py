"""``corsia event emit`` — submit declared local events through the common intake.

For programs on this machine only: the CLI acts as whichever session it is told, like
``corsia artifact``. A remote producer (a CI step elsewhere) uses its connector's webhook receiver.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, NoReturn


logger = logging.getLogger(__name__)


class _EmitParser(argparse.ArgumentParser):
    """Keep emit usage failures to the documented one-line stderr shape."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, f"error: usage: {message}. Run corsia event emit --help.\n")


def _read_facts(raw: str) -> dict[str, Any] | Any:
    source = sys.stdin.read() if raw == "-" else raw
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        _print_error("bad_json", "facts must be valid JSON")
        raise SystemExit(1) from None


def _print_error(code: str, text: str, fix: str | None = None) -> None:
    from events.intake import format_event_error

    print(f"error: {format_event_error(code, text, fix)}", file=sys.stderr)


def _cmd_emit(args: argparse.Namespace) -> int:
    from events.intake import admit_event, emitted_event_id, failure_code, record_refusal

    session = args.as_thread or os.environ.get("CORSIA_THREAD_ID")
    if not session:
        print("error: usage: pass --as-session or set CORSIA_THREAD_ID. Run corsia event emit --help.", file=sys.stderr)
        return 2
    facts = _read_facts(args.facts)
    event_id = emitted_event_id(args.event_type, args.identity)
    try:
        result = asyncio.run(admit_event(
            event_type=args.event_type, facts=facts, origin="emitter",
            producer=session, event_id=event_id,
        ))
    except Exception as exc:  # noqa: BLE001 - split into retryable and defect below
        if failure_code(exc) == "internal_error":
            logger.exception("corsia event emit failed inside the intake")
            _print_error("internal_error", "the event intake failed unexpectedly")
            return 1
        _print_error(
            "intake_unreachable", "event intake is unavailable",
            None if args.identity else "Pass --id so a retry cannot duplicate the event.",
        )
        return 3
    if result.refusal is not None:
        try:
            record_refusal(
                event_id=event_id, event_type=args.event_type, origin="emitter",
                producer=session, refusal=result.refusal,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record CLI event refusal")
        _print_error(result.refusal.code, result.refusal.text)
        return 1
    print(json.dumps({
        "event_id": event_id,
        "duplicate": result.duplicate,
        "rules_matched": result.rules_matched,
    }, ensure_ascii=False))
    return 0


def build_parser(subparsers: argparse._SubParsersAction) -> None:
    """Attach the declared-event command group to the CORSIA CLI."""
    event = subparsers.add_parser("event", help="Emit a declared event from a local program.")
    verbs = event.add_subparsers(dest="event_verb", required=True, parser_class=_EmitParser)
    emit = verbs.add_parser("emit", help="Emit one declared event through the live intake.")
    emit.add_argument("event_type", help="Declared event type, such as qa.review_completed.")
    emit.add_argument("--facts", required=True, help="JSON object, or - to read one JSON object from stdin.")
    emit.add_argument("--id", dest="identity", help="Stable identity for a retry-safe event.")
    emit.add_argument("--as-session", dest="as_thread", help="Chat id the event is emitted as (default: $CORSIA_THREAD_ID).")
    emit.set_defaults(func=_cmd_emit)
