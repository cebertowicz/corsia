"""Ad-hoc analytics query CLI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _add_backend_to_path() -> None:
    """Resolve the backend package and add it to ``sys.path``.

    The CLI is invoked from arbitrary directories (cron, agent shells,
    an interactive terminal). We find the repo root by walking up from this
    file's location until we see ``backend/infra/analytics.py``.
    """
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents):
        if (parent / "backend" / "infra" / "analytics.py").exists():
            sys.path.insert(0, str(parent / "backend"))
            return
    fallback = Path.home() / ".corsia" / "backend"
    if (fallback / "infra" / "analytics.py").exists():
        sys.path.insert(0, str(fallback))


_add_backend_to_path()

from infra import analytics  # noqa: E402  (path tweak above)
from infra import analytics_db  # noqa: E402


def _parse_since(s: str | None) -> timedelta | None:
    """Translate ``'7d'`` / ``'24h'`` / ``'all'`` / ``None`` into a
    ``timedelta`` (or ``None`` for "no lower bound").

    Suffixes: ``d`` (days), ``h`` (hours), ``m`` (minutes). The literal
    ``"all"`` returns ``None`` — passes through to the helpers as
    "unbounded".
    """
    if s is None or s.lower() == "all":
        return None
    s = s.strip().lower()
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    raise ValueError(
        f"unrecognised --since value {s!r}: use Nd/Nh/Nm or 'all'"
    )


def _parse_filters(s: str | None) -> dict[str, str]:
    """Parse a ``"key=value,key=value"`` string into a dict.

    Empty / None returns ``{}``. Whitespace around tokens is trimmed.
    Values are taken verbatim (no type coercion); helpers handle
    JSON-vs-column routing.
    """
    if not s:
        return {}
    out: dict[str, str] = {}
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(
                f"filter token missing '=' separator: {token!r}"
            )
        k, _, v = token.partition("=")
        out[k.strip()] = v.strip()
    return out


def _emit_tsv(headers: list[str], rows: list[tuple]) -> None:
    """Write TSV to stdout with a header line."""
    print("\t".join(headers))
    for row in rows:
        print("\t".join(_render_cell(v) for v in row))


def _render_cell(v: object) -> str:
    """Stringify a cell value for TSV output."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return str(v)


def _emit_json(payload: object) -> None:
    """Write payload as JSON to stdout.

    ``datetime`` is encoded via the default str() form (ISO-ish);
    helpers that return datetimes (e.g., ``time_series``) tag their
    rows so consumers can re-parse.
    """
    print(json.dumps(payload, default=str, indent=2))


def _cmd_sql(args: argparse.Namespace) -> int:
    """Run a raw SQL query against the analytics DB."""
    conn = analytics_db.get_db()
    cur = conn.execute(args.sql)
    columns = [d[0] for d in (cur.description or [])]
    rows = cur.fetchall()
    if args.json:
        _emit_json([dict(zip(columns, row)) for row in rows])
    else:
        _emit_tsv(columns, rows)
    return 0


def _cmd_funnel(args: argparse.Namespace) -> int:
    """Run a funnel query through the helper."""
    steps = [s.strip() for s in args.funnel.split(",") if s.strip()]
    since = _parse_since(args.since)
    filters = _parse_filters(args.filters)
    result = analytics.funnel(steps, since=since, filters=filters or None)
    if args.json:
        _emit_json(result)
    else:
        _emit_tsv(["step", "count"], list(result.items()))
    return 0


def _cmd_before_after(args: argparse.Namespace) -> int:
    """Run a before/after comparison through the helper."""
    filters = _parse_filters(args.before_after)
    cutoff = datetime.fromisoformat(args.cutoff)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    metric = args.metric or "properties->>'value'"
    result = analytics.before_after(
        event_filter=filters,
        cutoff_ts=cutoff,
        metric=metric,
    )
    if args.json:
        _emit_json(result)
    else:
        _emit_tsv(["side", "n", "mean", "median", "p95", "sd"], [
            ("before", result["before"]["n"], result["before"]["mean"],
             result["before"]["median"], result["before"]["p95"],
             result["before"]["sd"]),
            ("after", result["after"]["n"], result["after"]["mean"],
             result["after"]["median"], result["after"]["p95"],
             result["after"]["sd"]),
        ])
        print(
            f"\nt={result['ttest']['t']!r}  p={result['ttest']['p']!r}  "
            f"effect_size={result['ttest']['effect_size']!r}",
            file=sys.stderr,
        )
    return 0


def _cmd_top_n(args: argparse.Namespace) -> int:
    """Run a top-N leaderboard through the helper."""
    since = _parse_since(args.since)
    filters = _parse_filters(args.filters)
    rows = analytics.top_n(
        group_by=args.top_n,
        metric=args.metric or "*",
        n=args.n,
        since=since,
        filters=filters or None,
        agg=args.agg,
    )
    if args.json:
        _emit_json(rows)
    else:
        _emit_tsv(["group", "value"], rows)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Subcommands are flag-selected rather than positional so a single
    invocation can mix ``--since`` / ``--filters`` etc consistently.
    The mode is determined by which of ``--sql`` / ``--funnel`` /
    ``--before-after`` / ``--top-n`` is set; exactly one must be.
    """
    p = argparse.ArgumentParser(
        prog="qh query",
        description=(
            "Ad-hoc analytics queries against ~/.corsia/analytics.duckdb. "
            "Sprint 5 Slice 0 (DECISIONS-LOG #26)."
        ),
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--sql",
        help="Raw SQL to run; column headers preserved in output.",
    )
    mode.add_argument(
        "--funnel",
        help="Comma-separated event names, e.g. 'workflow.start,spec.approved'.",
    )
    mode.add_argument(
        "--before-after",
        dest="before_after",
        help=(
            "Filter dict for before-after compare, "
            "e.g. 'category=ci,action=ci_pass'. Pair with --cutoff + --metric."
        ),
    )
    mode.add_argument(
        "--top-n",
        dest="top_n",
        help="Column or JSON path to group by (e.g. 'category', or "
             "'properties->>\\'model\\''). Pair with --n, --metric, --agg.",
    )

    p.add_argument(
        "--since",
        help="Lower bound for ts, e.g. '7d', '24h', '30m', 'all'. "
             "Default: 30d for helpers, no bound for --sql.",
    )
    p.add_argument(
        "--filters",
        help="Row filters as comma-separated key=value pairs.",
    )

    p.add_argument(
        "--cutoff",
        help="ISO-8601 cutoff timestamp for --before-after. "
             "UTC assumed when no tz suffix.",
    )
    p.add_argument(
        "--metric",
        help="Metric column or JSON path (e.g. \"properties->>'duration_ms'\").",
    )

    p.add_argument("--n", type=int, default=10, help="Top-N cap (default 10).")
    p.add_argument(
        "--agg",
        choices=("count", "sum", "avg"),
        default="count",
        help="Aggregation for --top-n (default count).",
    )

    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON; default is TSV.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _build_parser().parse_args(argv)
    try:
        if args.sql:
            return _cmd_sql(args)
        if args.funnel:
            return _cmd_funnel(args)
        if args.before_after:
            if not args.cutoff:
                print("--before-after requires --cutoff", file=sys.stderr)
                return 2
            return _cmd_before_after(args)
        if args.top_n:
            return _cmd_top_n(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"qh query: error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
