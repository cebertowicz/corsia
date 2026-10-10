"""Command line: measure a tree, compare with a baseline, raise the baseline, or gate two trees.

    python -m speed_gate.cli measure --backend backend --out measurement.json
    python -m speed_gate.cli compare --current measurement.json --baseline quality/speed-gate-baseline.json
    python -m speed_gate.cli update-baseline --current measurement.json --baseline ... --reason "why"
    python -m speed_gate.cli gate --head-backend backend --base-backend /path/to/merge-base/backend

Exit codes: 0 pass (or would-fail in report-only mode), 1 would-fail with --enforce,
2 version mismatch with --enforce (report-only warns and exits 0), 3 the two runs of one tree disagreed
beyond the band with --enforce (report-only warns and exits 0), 4 the fixture build or a harness run failed.
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
    """The statements one run executed for ``route`` that another did not, per run, with the thread they ran on."""
    traces = [(run.get("routes", {}).get(route) or {}).get("info", {}).get("trace", []) for run in runs]
    lines = []
    for number, trace in enumerate(traces, 1):
        others = collections.Counter(line for index, other in enumerate(traces, 1) if index != number for line in other)
        own = collections.Counter(trace)
        lines += [f"    only in run {number}: {line} (x{count - others[line]})" for line, count in own.items() if count > others[line]]
    return lines


def _band_line(item: ratchet.Disagreement) -> str:
    if item.metric == "measured":
        return f"{item.route}: measured in only one run (steps run 1: {item.first}, run 2: {item.second})"
    verdict = "within the band" if item.within_band else "OUTSIDE the band"
    return (f"{item.route}: {item.metric} run 1 {item.first:,}, run 2 {item.second:,}, "
            f"difference {abs(item.first - item.second):,}, band {item.band:,}: {verdict}")


def _instability_summary(unstable: list[ratchet.Disagreement], tolerated: int, enforce: bool) -> str:
    mode = "enforcing: this fails the job" if enforce else "report-only: the job stays green"
    lines = ["## Read-cost speed gate: the two runs of this tree disagreed", "",
             f"{len(unstable)} count(s) differ by more than the run-to-run band ({mode}). The band is "
             f"{ratchet.RUN_BAND:.0%} of the larger run (at least {ratchet.STEP_SLACK} steps); statements must match exactly. "
             f"{tolerated} more differ inside the band.", "",
             "| Route | Metric | Run 1 | Run 2 | Band |", "|---|---|---|---|---|"]
    for item in unstable:
        first = "-" if item.first is None else f"{item.first:,}"
        second = "-" if item.second is None else f"{item.second:,}"
        lines.append(f"| `{item.route}` | {item.metric} | {first} | {second} | {item.band:,} |")
    return "\n".join(lines) + "\n"


def measure_tree(backend: Path, repeats: int, keep_runs: Path | None = None, *, enforce: bool = False,
                 summary: Path | None = None) -> tuple[dict, int]:
    """Measure ``backend``; with two or more runs, compare them against the run-to-run band.

    Statements must match exactly and steps within ``ratchet.RUN_BAND``. Every difference is printed with
    both numbers and its band; one outside the band is instability in the gate itself. Report-only prints it
    as a warning and in the job summary and still returns 0; only ``enforce`` returns exit 3. For a route
    outside the band each run's counts and the statements the runs did not share are printed and, with
    ``keep_runs``, every run is written beside it as ``<keep_runs>.run<N>.json``.
    """
    runs, manifest, timings = runner.build_and_measure(backend, repeats)
    first = runs[0]
    first["fixture_manifest"] = {key: manifest[key] for key in ("size_bytes", "sqlite_version", "builder_sha256", "schema")}
    first["info"] = timings
    differences = [item for other in runs[1:] for item in ratchet.compare_runs(first, other)]
    if not differences:
        return first, 0
    unstable = [item for item in differences if not item.within_band]
    print(f"Two runs of the same tree differ on {len(differences)} count(s); {len(unstable)} outside the band "
          f"({ratchet.RUN_BAND:.0%} of the larger run, at least {ratchet.STEP_SLACK} steps; statements exact):", file=sys.stderr)
    for item in differences:
        print(f"  {_band_line(item)}", file=sys.stderr)
    if not unstable:
        return first, 0
    print("NONDETERMINISTIC: counts differ between two runs of the same tree beyond the band:", file=sys.stderr)
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
            handle.write(_instability_summary(unstable, len(differences) - len(unstable), enforce))
    if enforce:
        return first, EXIT_NONDETERMINISTIC
    print("::warning title=Read-cost speed gate::the two runs of one tree disagreed beyond the band; "
          "report-only, so this is not a failure (it would fail with --enforce)")
    return first, 0


def decide(current: dict, baseline: dict, *, enforce: bool, base_label: str, summary: Path | None) -> int:
    verdict = ratchet.compare(current, baseline)
    text = report.render(current, verdict, baseline, report_only=not enforce, base_label=base_label)
    print(text)
    if summary:
        with summary.open("a") as handle:
            handle.write(text + "\n")
    if verdict.status == "version_mismatch":
        # Not a verdict, but report-only phase must not turn CI red: warn loudly, fail only when enforcing.
        print(f"::warning title=Read-cost speed gate::{verdict.message}")
        return EXIT_REBASELINE if enforce else 0
    return EXIT_WOULD_FAIL if verdict.status == "would_fail" and enforce else 0


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


def _command_gate(args: argparse.Namespace) -> int:
    """Measure the merge-base and the head on this machine; the base is the baseline."""
    head, code = measure_tree(args.head_backend, args.repeats, enforce=args.enforce, summary=args.summary)
    base, base_code = measure_tree(args.base_backend, 1, enforce=args.enforce, summary=args.summary)
    if code or base_code:
        return code or base_code
    if args.out:
        args.out.write_text(json.dumps(head, indent=2, sort_keys=True))
    return decide(head, ratchet.new_baseline(base, "merge-base measured on this machine"),
                  enforce=args.enforce, base_label="the merge-base measured on this machine", summary=args.summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speed_gate")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure")
    measure.add_argument("--backend", required=True, type=Path)
    measure.add_argument("--out", required=True, type=Path)
    measure.add_argument("--repeats", type=int, default=2, help="independent harness runs; they must agree within the band")
    measure.add_argument("--summary", type=Path, help="append a disagreement report here (the job summary file)")
    measure.add_argument("--enforce", action="store_true", help="exit 3 when the runs disagree beyond the band; default is report-only")
    measure.set_defaults(run=_command_measure)
    compare = sub.add_parser("compare")
    compare.add_argument("--current", required=True, type=Path)
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--summary", type=Path, help="append the report here (the job summary file)")
    compare.add_argument("--enforce", action="store_true", help="exit 1 on would-fail; default is report-only")
    compare.set_defaults(run=_command_compare)
    update = sub.add_parser("update-baseline")
    update.add_argument("--current", required=True, type=Path)
    update.add_argument("--baseline", required=True, type=Path)
    update.add_argument("--reason", required=True)
    update.set_defaults(run=_command_update)
    gate = sub.add_parser("gate")
    gate.add_argument("--head-backend", required=True, type=Path)
    gate.add_argument("--base-backend", required=True, type=Path)
    gate.add_argument("--repeats", type=int, default=1)
    gate.add_argument("--out", type=Path)
    gate.add_argument("--summary", type=Path)
    gate.add_argument("--enforce", action="store_true")
    gate.set_defaults(run=_command_gate)
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
