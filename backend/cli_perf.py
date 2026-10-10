"""Historical aggregate performance reports for the CORSIA CLI."""

from __future__ import annotations

import argparse
import fnmatch
import sys

from infra.product_events import analytics_disabled
from infra.request_timing import bucket_percentile, window_start
from scripts import corsia_api

_MAX_DAYS = 90


def _days(value: str) -> int:
    """Parse a bounded positive historical window."""
    try:
        days = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("days must be an integer") from error
    if not 1 <= days <= _MAX_DAYS:
        raise argparse.ArgumentTypeError(f"days must be between 1 and {_MAX_DAYS}")
    return days


def _window_label(days: int) -> str:
    return f"UTC days {window_start(days).isoformat()} through today"


def _print_empty(label: str, days: int) -> int:
    print(f"No {label} data for {_window_label(days)}.")
    print("Timing may be new, disabled, or not yet flushed; this is not zero latency.")
    return 0


def cmd_routes(args: argparse.Namespace) -> int:
    """Print daily historical route timing from the running server, by p95 descending."""
    if analytics_disabled():
        print("Route timing analytics is disabled (CORSIA_ANALYTICS_DISABLE=1).", file=sys.stderr)
        return 2
    base = corsia_api.resolve_base(None)
    token = corsia_api.resolve_token(None)
    try:
        payload = corsia_api.request(
            "GET", base, "/api/system/perf/routes", token, query={"days": args.days},
        )
    except SystemExit:  # the shared client already printed the HTTP or connection error
        print(f"The route timing report needs the running CORSIA server at {base}.", file=sys.stderr)
        return 2
    rows = [
        row for row in payload["rows"]
        if args.route is None or fnmatch.fnmatchcase(row[2], args.route)
    ]
    if not rows:
        return _print_empty("route timing", args.days)
    rendered = [
        (row[0], row[1], row[2], row[3], int(row[4]),
         min(bucket_percentile(row[6], 0.50), float(row[5])),
         min(bucket_percentile(row[6], 0.95), float(row[5])),
         min(bucket_percentile(row[6], 0.99), float(row[5])), float(row[5]))
        for row in rows
    ]
    rendered.sort(key=lambda row: row[6], reverse=True)
    print(f"Historical route timing ({_window_label(args.days)}; ms; aggregate-only; sorted by p95 descending)")
    print("date       method route                          status count p50    p95    p99    max")
    for day, method, route, status, count, p50, p95, p99, maximum in rendered:
        print(f"{day} {method:<6} {route:<30} {status:<6} {count:>5} {p50:>6.1f} {p95:>6.1f} {p99:>6.1f} {maximum:>6.1f}")
    return 0


def _aggregate_row(db, query: str, start: str):
    return db.execute(query, (start,)).fetchone()


def cmd_tools(args: argparse.Namespace) -> int:
    """Print aggregate tool and first-token latency from existing SQLite rows."""
    start = window_start(args.days).isoformat()
    try:
        from infra.database import get_db

        db = get_db()
        try:
            rows = [
                ("message tool result persistence (approximate)", _aggregate_row(db, """
                    SELECT COUNT(*), AVG((julianday(tool.created_at) - julianday(step.created_at)) * 86400000.0),
                    MAX((julianday(tool.created_at) - julianday(step.created_at)) * 86400000.0)
                    FROM messages AS tool JOIN messages AS step
                    ON step.id = json_extract(tool.metadata, '$.assistant_step_id')
                    WHERE tool.role='tool' AND json_valid(tool.metadata) AND tool.created_at >= ?
                """, start)),
                ("plan tool dispatch", _aggregate_row(db, """
                    SELECT COUNT(*), AVG((julianday(settled_at) - julianday(started_at)) * 86400000.0),
                    MAX((julianday(settled_at) - julianday(started_at)) * 86400000.0)
                    FROM plan_tool_dispatches WHERE started_at >= ? AND settled_at IS NOT NULL
                """, start)),
                ("LLM time to first token", _aggregate_row(db, """
                    SELECT COUNT(*), AVG(ttft_ms), MAX(ttft_ms) FROM llm_usage
                    WHERE created_at >= ? AND ttft_ms IS NOT NULL
                """, start)),
            ]
        finally:
            db.close()
    except Exception as error:
        print(f"Tool timing data unavailable: {error}", file=sys.stderr)
        return 2
    if not any(row[1][0] for row in rows):
        return _print_empty("tool timing", args.days)
    print(f"Historical tool timing ({_window_label(args.days)}; ms; aggregate-only)")
    print("metric                                         count avg_ms max_ms")
    for label, (count, average, maximum) in rows:
        print(f"{label:<46} {count:>5} {float(average or 0):>6.1f} {float(maximum or 0):>6.1f}")
    return 0


def build_parser(subparsers) -> None:
    """Register historical aggregate performance commands, not active inventory."""
    perf = subparsers.add_parser("perf", help="Historical aggregate performance reports")
    commands = perf.add_subparsers(dest="perf_cmd", required=True)
    days_help = "Inclusive UTC day window: today plus the previous N-1 days (1-90; default: 7)"
    routes = commands.add_parser("routes", help="Historical route latency; needs the running server")
    routes.add_argument("--days", type=_days, default=7, help=days_help)
    routes.add_argument("--route", help="Normalized route-template glob, for example /api/threads/*")
    routes.set_defaults(func=cmd_routes)
    tools = commands.add_parser("tools", help="Historical aggregate tool and LLM latency")
    tools.add_argument("--days", type=_days, default=7, help=days_help)
    tools.set_defaults(func=cmd_tools)
