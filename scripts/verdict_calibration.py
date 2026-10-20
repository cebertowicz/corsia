#!/usr/bin/env python3
"""Calibrate the workflow verdict on real history, read-only, printing aggregates only (verdict design §9 row 1).

    scripts/verdict_calibration.py /path/to/clone.db

Opens the database with ``mode=ro`` and runs the verdict's own read side (``core.workflow_verdict_read``), pricing
included, and its own bootstrap. Prints no run, message or node: only

1. **A/A splits.** Each version of ``sdlc-process`` and ``decision-brief`` is split once, into the earlier and later
   halves of its runs in start order, and the later half is judged against the earlier by the production rule, so
   each version gives one sample. Per measure, over the splits it could measure (a split with no baseline, too few
   runs or nothing comparable is counted in ``splits`` but never in the share): the share that did not read Worse, and
   the 90th percentile of the split differences, which replaces the 25% margin when that share is under 90%. A
   measure no split could measure shows ``-`` and is listed as not calibrated.
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
from core.workflow_verdict_rules import (  # noqa: E402
    WorkflowHistory, compute, decide, freeze_plan, sides,
)
from core.workflow_verdicts import measure_call, parse_ts  # noqa: E402

CALIBRATED = ("sdlc-process", "decision-brief")


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


def _split(history: WorkflowHistory, version: str) -> WorkflowHistory | None:
    """The version's runs as earlier half "a" and later half "b" in start order; None below two runs."""
    own = sorted((r for r in history.runs if r.version == version), key=lambda r: (parse_ts(r.started_at), r.run_id))
    midpoint = len(own) // 2
    halves = [replace(run, version="a" if index < midpoint else "b") for index, run in enumerate(own)]
    if {run.version for run in halves} != {"a", "b"}:
        return None
    return replace(history, ref=f"{history.ref}#{version}", runs=tuple(halves))


def owner_kpis(history: WorkflowHistory) -> set[str]:
    """The deciding owner KPIs held by any plan this script builds (every A/A split and every replay), using the
    definitions in force at each plan's first use, as production does."""
    plans = [freeze_plan(history, version) for version in history.versions()]
    plans += [freeze_plan(split, "b") for version in history.versions() if (split := _split(history, version))]
    return {measure.name for plan in plans for measure in plan.measures if measure.source == "kpi"}


def uncalibrated(ref: str, calls: dict, kpis: set[str]) -> list[str]:
    """Measures no split could measure: those every split left uncalled, and deciding owner KPIs no split held."""
    names = {name for name, found in calls.items() if not aa_share(found)[0]} | (kpis - set(calls))
    return [f"{ref}/{name}" for name in sorted(names)]


def aa_share(found: list[tuple[str, float | None]]) -> tuple[int, float | None]:
    """The splits a measure could measure, and the share of them that did not read Worse."""
    measured = [call for call, diff in found if diff is not None]
    return len(measured), (sum(call != "worse" for call in measured) / len(measured) if measured else None)


def splits(history: WorkflowHistory, now: datetime) -> tuple[dict, list[tuple[str, int]]]:
    """Per measure: calls and differences over chronological A/A halves; plus each half's verdict word."""
    calls: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
    words: list[tuple[str, int]] = []
    for version in history.versions():
        split = _split(history, version)
        if split is None:
            continue
        plan = freeze_plan(split, "b")
        words.append((decide(split, plan, now).word, len(split.runs)))
        if plan.baseline is None:
            for measure in plan.measures:
                calls[measure.name].append(("no_baseline", None))
            continue
        old, new = sides(split, plan)
        for measure in plan.measures:
            call = measure_call(measure, new, old, minimum=plan.minimum, now=now, seed=plan.seed + len(measure.name))
            calls[measure.name].append((call.call, call.difference))
    return calls, words


def correction_lags(histories: list[WorkflowHistory]) -> list[float]:
    lags = []
    for history in histories:
        for run in history.runs:
            facts = [parse_ts(fact.source_at) for fact in run.corrections if fact.is_correction]
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
    no_history = [ref for ref in CALIBRATED if ref not in {history.ref for history in calibrated}]
    if no_history:
        raise SystemExit("missing calibrated workflow history for calibration: " + ", ".join(no_history))
    missing_definitions = [history.ref for history in calibrated
                           if not history.owner or not owner_kpis(history)]
    if missing_definitions:
        raise SystemExit("missing owner KPI definitions in force at first use for calibration in CORSIA_WORKSPACE: "
                         + ", ".join(missing_definitions))

    print("A/A splits: of the splits each measure could measure, the share not reading Worse, and p90 |difference|")
    print(f"{'workflow':<16} {'measure':<26} {'splits':>6} {'called':>6} {'not worse':>9} {'p90 diff':>9}")
    missing = []
    for history in calibrated:
        calls, words = splits(history, now)
        missing += uncalibrated(history.ref, calls, owner_kpis(history))
        for name, found in sorted(calls.items()):
            called, quiet = aa_share(found)
            differences = [abs(diff) for _, diff in found if diff is not None]
            print(f"{history.ref:<16} {name:<26} {len(found):>6} {called:>6} {_fmt(quiet, 2):>9} "
                  f"{_fmt(percentile(differences, 0.9)):>9}")
        reached = [word for word, _ in words if word in {"keep", "revert"}]
        share = sum(word == "keep" for word in reached) / len(reached) if reached else None
        print(f"{history.ref:<16} {'(whole verdict)':<26} {len(words):>6} {len(reached):>6} {_fmt(share, 2):>9}")
    print(f"Not calibrated (no split could measure it): {', '.join(missing) or 'none'}")

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
