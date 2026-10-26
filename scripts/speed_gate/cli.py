"""Command line: measure a tree, compare it with the committed baseline, or raise the baseline.

    python -m speed_gate.cli measure --backend backend --out measurement.json
    python -m speed_gate.cli compare --current measurement.json --baseline quality/speed-gate-baseline.json
    python -m speed_gate.cli update-baseline --current measurement.json --baseline ... --reason "why"

CI and ``scripts/check-pr`` run ``measure`` and ``compare`` with ``--enforce`` against the same committed
baseline. Exit codes: 0 pass, 1 a would-fail or improved route (update the baseline in the same PR with a
reason), 2 a SQLite version mismatch, 3 runs of one tree disagree, or 4 the fixture build or a harness run
failed or a measurement guard stopped the run (no verdict).
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from . import ratchet, report, runner

EXIT_WOULD_FAIL, EXIT_REBASELINE, EXIT_NONDETERMINISTIC, EXIT_BROKEN = 1, 2, 3, 4


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _describe(run: dict, route: str) -> str:
    cost = ratchet.measured(run).get(route)
    return "not measured" if cost is None else f"steps {cost['steps']:,}, statements {cost['statements']}, bytes {cost['bytes']:,}"


def _trace_difference(runs: list[dict], route: str) -> list[str]:
    """How the runs' statements for ``route`` differ, each with the thread it ran on.

    The statements one run executed that another did not, per run; and a run that executed the same
    statements as run 1 in another order, from the first position where they part. Order matters on its own:
    work moved across a transaction boundary, or onto another connection, can cost a different amount.
    """
    traces = [(run.get("routes", {}).get(route) or {}).get("info", {}).get("trace", []) for run in runs]
    lines = []
    for number, trace in enumerate(traces, 1):
        others = collections.Counter(line for index, other in enumerate(traces, 1) if index != number for line in other)
        own = collections.Counter(trace)
        lines += [f"    only in run {number}: {line} (x{count - others[line]})" for line, count in own.items() if count > others[line]]
    first = traces[0]
    for number, trace in enumerate(traces[1:], 2):
        if trace == first or collections.Counter(trace) != collections.Counter(first):
            continue
        moved = [index for index, (one, other) in enumerate(zip(first, trace)) if one != other]
        lines.append(f"    order differs in run {number} at {len(moved)} of {len(first)} statements, from statement "
                     f"{moved[0] + 1}: run 1 ran {first[moved[0]]}; run {number} ran {trace[moved[0]]}")
    return lines


def _disagreement_line(item: ratchet.Disagreement) -> str:
    if item.metric == "measured":
        return f"{item.route}: measured in only one run (steps run 1: {item.first}, run 2: {item.second})"
    verdict = f"within its tolerance of {item.tolerance} ({item.reason})" if item.tolerated else "NOT tolerated"
    return (f"{item.route}: {item.metric} run 1 {item.first:,}, run 2 {item.second:,}, "
            f"difference {abs(item.first - item.second):,}: {verdict}")


def _instability_summary(unstable: list[ratchet.Disagreement], tolerated: int, runs: int, enforce: bool) -> str:
    mode = "enforcing: this fails the job" if enforce else "report-only: the job stays green"
    lines = ["## Read-cost speed gate: runs of this tree disagreed", "",
             f"{len(unstable)} count(s) differ between {runs} runs of one tree ({mode}). Steps and statements must be "
             f"identical, and bytes too except on a route with a committed byte tolerance; {tolerated} more differ "
             "inside one.", "",
             "| Route | Metric | Run 1 | Run 2 |", "|---|---|---|---|"]
    for item in unstable:
        first = "-" if item.first is None else f"{item.first:,}"
        second = "-" if item.second is None else f"{item.second:,}"
        lines.append(f"| `{item.route}` | {item.metric} | {first} | {second} |")
    return "\n".join(lines) + "\n"


def measure_tree(backend: Path, repeats: int, keep_runs: Path | None = None, *, enforce: bool = False,
                 summary: Path | None = None) -> tuple[dict, int]:
    """Measure ``backend``; with two or more runs, every run must equal the first.

    Steps and statements must match exactly, bytes too except where ``ratchet.BYTE_TOLERANCE`` names the
    route. Every difference is printed with both numbers; one that is not tolerated is instability in the
    gate itself. Report-only prints it as a warning and in the job summary and still returns 0; only
    ``enforce`` returns exit 3. For such a route each run's counts and the statements the runs did not
    share are printed and, with ``keep_runs``, every run is written beside it as ``<keep_runs>.run<N>.json``.
    """
    runs, manifest, timings = runner.build_and_measure(backend, repeats)
    first = runs[0]
    first["fixture_manifest"] = {key: manifest[key] for key in
                                 ("size_bytes", "sqlite_version", "builder_sha256", "schema", "excluded", "short")}
    first["info"] = {**timings, "repeats": len(runs)}
    differences = [item for other in runs[1:] for item in ratchet.compare_runs(first, other)]
    if not differences:
        return first, 0
    unstable = [item for item in differences if not item.tolerated]
    print(f"Runs of the same tree differ on {len(differences)} count(s); {len(unstable)} not tolerated "
          "(steps and statements exact; bytes exact except where a route has a committed tolerance):", file=sys.stderr)
    for item in differences:
        print(f"  {_disagreement_line(item)}", file=sys.stderr)
    if not unstable:
        return first, 0
    print("NONDETERMINISTIC: counts differ between runs of the same tree:", file=sys.stderr)
    for route in sorted({item.route for item in unstable}):
        print(f"  {route}", file=sys.stderr)
        for number, run in enumerate(runs, 1):
            print(f"    run {number}: {_describe(run, route)}", file=sys.stderr)
        for line in _trace_difference(runs, route):
            print(line, file=sys.stderr)
    if keep_runs:
        for number, run in enumerate(runs, 1):
            Path(f"{keep_runs}.run{number}.json").write_text(json.dumps(run, indent=2, sort_keys=True))
    if summary:
        with summary.open("a") as handle:
            handle.write(_instability_summary(unstable, len(differences) - len(unstable), len(runs), enforce))
    if enforce:
        return first, EXIT_NONDETERMINISTIC
    routes = ", ".join(sorted({item.route for item in unstable}))
    print(f"speed-gate: runs of this tree disagreed on {routes}")
    print("::warning title=Read-cost speed gate::runs of one tree disagreed; "
          "report-only, so this is not a failure (it would fail with --enforce)")
    return first, 0


def decide(current: dict, baseline: dict, *, enforce: bool, base_label: str, summary: Path | None) -> int:
    """Print the verdict. With ``enforce``, a would-fail or an improved row exits 1: the PR must update the baseline."""
    verdict = ratchet.compare(current, baseline)
    text = report.render(current, verdict, baseline, report_only=not enforce, base_label=base_label)
    print(text)
    if summary:
        with summary.open("a") as handle:
            handle.write(text + "\n")
    if verdict.status == "version_mismatch":
        # Not a verdict: non-enforcing callers warn loudly, enforcing callers fail with exit 2.
        print(f"speed-gate: rebaseline needed, {verdict.message}")
        print(f"::warning title=Read-cost speed gate::{verdict.message}")
        return EXIT_REBASELINE if enforce else 0
    unrecorded = verdict.status == "would_fail" or any(item.kind == "improved" for item in verdict.findings)
    for label, kinds in (("would fail", ("growth", "ceiling", "error")), ("baseline should drop", ("improved", "removed"))):
        routes = sorted({item.route for item in verdict.findings if item.kind in kinds})
        if routes:
            print(f"speed-gate: {label} on {', '.join(routes)}")
    return EXIT_WOULD_FAIL if unrecorded and enforce else 0


def _command_measure(args: argparse.Namespace) -> int:
    result, code = measure_tree(args.backend, args.repeats, keep_runs=args.out, enforce=args.enforce, summary=args.summary)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    return code


def _command_compare(args: argparse.Namespace) -> int:
    return decide(_load(args.current), _load(args.baseline), enforce=args.enforce,
                  base_label=str(args.baseline), summary=args.summary)


def _command_update(args: argparse.Namespace) -> int:
    if not args.reason.strip():
        print("a baseline change needs a one-line reason", file=sys.stderr)
        return 2
    old = _load(args.baseline) if args.baseline.exists() else {}
    fresh = ratchet.new_baseline(_load(args.current), args.reason.strip(), old.get("ceilings"))
    args.baseline.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
    print(f"baseline written: {len(fresh['routes'])} routes, {len(fresh['grandfathered'])} over a ceiling")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speed_gate")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure")
    measure.add_argument("--backend", required=True, type=Path)
    measure.add_argument("--out", required=True, type=Path)
    measure.add_argument("--repeats", type=int, default=2, help="independent harness runs; they must agree exactly")
    measure.add_argument("--summary", type=Path, help="append a disagreement report here (the job summary file)")
    measure.add_argument("--enforce", action="store_true", help="exit 3 when runs of one tree disagree")
    measure.set_defaults(run=_command_measure)
    compare = sub.add_parser("compare")
    compare.add_argument("--current", required=True, type=Path)
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--summary", type=Path, help="append the report here (the job summary file)")
    compare.add_argument("--enforce", action="store_true",
                         help="exit 1 on a would-fail or improved route; the report gives the rebaseline command")
    compare.set_defaults(run=_command_compare)
    update = sub.add_parser("update-baseline")
    update.add_argument("--current", required=True, type=Path)
    update.add_argument("--baseline", required=True, type=Path)
    update.add_argument("--reason", required=True)
    update.set_defaults(run=_command_update)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except runner.MeasurementError as error:
        print(f"speed gate could not measure: {error}", file=sys.stderr)
        return EXIT_BROKEN


if __name__ == "__main__":
    raise SystemExit(main())
