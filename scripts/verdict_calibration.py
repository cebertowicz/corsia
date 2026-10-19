#!/usr/bin/env python3
"""Calibrate the workflow verdict on real history, read-only, printing aggregates only (verdict design §9 row 1).

    scripts/verdict_calibration.py /path/to/clone.db

Opens the database with ``mode=ro`` and runs the verdict's own read side (``core.workflow_verdict_read``), pricing
included, and its own bootstrap. Prints no run, message or node: only

1. **A/A splits.** Each version of ``sdlc-process`` and ``decision-brief`` is split into alternating halves (blocks
   of 1 to 5 runs in start order, one split per block size) and the later half is judged against the earlier. Per
   measure: the share of splits that read Keep or Not enough data (any call but Worse), and the 90th percentile of
   the split differences, which replaces the 25% margin when that share is under 90%.
2. **Correction lag.** Days from a run settling to its last attributed correction: p50, p90, p95, max.
3. **Replay.** Each real version against its baseline: the verdict and reason it would have given.

Run it with ``CORSIA_WORKSPACE`` pointed at the real workspace whose owner workflows and KPI definitions are being calibrated;
``CORSIA_STATE_DIR`` may be throwaway and defaults to a fresh temporary folder.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("CORSIA_STATE_DIR", tempfile.mkdtemp(prefix="verdict-calibration-"))
os.environ.setdefault("CORSIA_ANALYTICS_DISABLE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core.workflow_verdict_read import price_snapshot, read_snapshot  # noqa: E402
from core.workflow_verdict_rules import WorkflowHistory, baseline_runs, compute, decide, freeze_plan  # noqa: E402
from core.workflow_verdicts import measure_call, parse_ts  # noqa: E402

CALIBRATED = ("sdlc-process", "decision-brief")
BLOCKS = range(1, 6)


def connect(path: str) -> sqlite3.Connection:
    """Open an existing database without ever creating or changing it."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(f"not a database file: {resolved}")
    db = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def splits(history: WorkflowHistory, now: datetime) -> tuple[dict, list[tuple[str, int]]]:
    """Per measure: calls and differences over every A/A split; plus each split's whole verdict word."""
    calls: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
    words: list[tuple[str, int]] = []
    for version in history.versions():
        own = sorted((r for r in history.runs if r.version == version), key=lambda r: (parse_ts(r.started_at), r.run_id))
        for block in BLOCKS:
            halves = [replace(run, version="a" if (index // block) % 2 == 0 else "b") for index, run in enumerate(own)]
            split = replace(history, ref=f"{history.ref}#{version}#{block}", runs=tuple(halves))
            if {run.version for run in halves} != {"a", "b"}:
                continue
            plan = freeze_plan(split, "b", now)
            words.append((decide(split, plan, now).word, len(own)))
            if plan.baseline is None:
                for measure in plan.measures:
                    calls[measure.name].append(("too_few", None))
                continue
            window = max(m.window_days for m in plan.measures)
            old = baseline_runs(split, "a", plan.minimum, window, now)
            new = [run for run in halves if run.version == "b"]
            for measure in plan.measures:
                call = measure_call(measure, new, old, minimum=plan.minimum, now=now, seed=plan.seed + len(measure.name))
                calls[measure.name].append((call.call, call.difference))
    return calls, words


def correction_lags(histories: list[WorkflowHistory]) -> list[float]:
    lags = []
    for history in histories:
        for run in history.runs:
            facts = [parse_ts(fact.message_at) for fact in run.corrections if fact.is_correction]
            if run.terminal_at and facts:
                lags.append(max(0.0, (max(facts) - parse_ts(run.terminal_at)).total_seconds() / 86_400))
    return lags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("db", help="path to a CORSIA SQLite database (opened read-only)")
    args = parser.parse_args(argv)
    db = connect(args.db)
    now = datetime.now(timezone.utc)
    snapshot = price_snapshot(db, read_snapshot(db, now=now))
    histories = list(snapshot.workflows)
    calibrated = [history for history in histories if history.ref in CALIBRATED]
    if not calibrated:
        raise SystemExit("missing calibrated workflow history for calibration")
    if not any(history.owner and any(definition.kind == "kpi" for definition in history.definitions)
               for history in calibrated):
        raise SystemExit("missing owner KPI definitions for calibration in CORSIA_WORKSPACE")

    print("A/A splits: share reading Keep or Not enough data, and p90 |difference|")
    print(f"{'workflow':<16} {'measure':<26} {'splits':>6} {'called':>6} {'keep/ned':>8} {'p90 diff':>9}")
    for history in histories:
        if history.ref not in CALIBRATED:
            continue
        calls, words = splits(history, now)
        for name, found in sorted(calls.items()):
            called = [diff for call, diff in found if diff is not None]
            quiet = sum(1 for call, _ in found if call != "worse") / len(found) if found else None
            print(f"{history.ref:<16} {name:<26} {len(found):>6} {len(called):>6} {_fmt(quiet, 2):>8} "
                  f"{_fmt(percentile([abs(d) for d in called], 0.9)):>9}")
        if words:
            share = sum(1 for word, _ in words if word not in {"revert"}) / len(words)
            print(f"{history.ref:<16} {'(whole verdict)':<26} {len(words):>6} {'':>6} {_fmt(share, 2):>8}")

    lags = correction_lags(histories)
    print("\nRun settle to last attributed correction, days")
    print(f"n={len(lags)} p50={_fmt(percentile(lags, 0.5), 2)} p90={_fmt(percentile(lags, 0.9), 2)} "
          f"p95={_fmt(percentile(lags, 0.95), 2)} max={_fmt(max(lags) if lags else None, 2)}")

    print("\nReplay: each version against its baseline")
    print(f"{'workflow':<16} {'baseline -> version':<29} {'verdict':<16} reason")
    for evaluation in compute(snapshot):
        workflow, _, version = evaluation.subject.removeprefix("workflow:").partition("@")
        pair = f"{(evaluation.plan.baseline or '-')[:12]} -> {version[:12]}"
        print(f"{workflow:<16} {pair:<29} {evaluation.verdict.word:<16} {evaluation.verdict.reason}")
    excluded = {h.ref: dict(h.excluded) for h in histories if h.excluded}
    print(f"\nRuns never judged (unversioned or local edit): {excluded or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
