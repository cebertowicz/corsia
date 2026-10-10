"""Markdown report for the job summary, a PR comment or the terminal."""

from __future__ import annotations

from . import ratchet

TOP_ROUTES = 15


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _cell(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def render(current: dict, verdict: ratchet.Verdict, baseline: dict, *, report_only: bool, base_label: str) -> str:
    ceilings = baseline.get("ceilings", ratchet.CEILINGS)
    mode = "report-only (does not block merging)" if report_only else "enforcing"
    lines = ["## Read-cost speed gate", "",
             f"Result: **{verdict.status.replace('_', ' ')}**, {mode}. SQLite {current['sqlite_version']}, "
             f"compared with {base_label}. The verdict uses only VM steps, statements and response bytes; "
             "wall time is never an input.", ""]
    if verdict.message:
        lines += [f"> {verdict.message}", ""]
    failing = verdict.failing
    if failing:
        lines += ["### Would fail", ""]
        lines += _table(["Route", "Metric", "Before", "After", "Why"],
                        [[f"`{item.route}`", item.metric, _cell(item.before), _cell(item.after), item.detail]
                         for item in failing])
        lines += [""]
    improved = [item for item in verdict.findings if item.kind == "improved"]
    if improved:
        lines += ["### Improved (lower the baseline in this PR)", ""]
        lines += _table(["Route", "Metric", "Before", "After"],
                        [[f"`{item.route}`", item.metric, _cell(item.before), _cell(item.after)] for item in improved])
        lines += [""]
    costly = ratchet.distribution(current)[:TOP_ROUTES]
    grandfathered = set(baseline.get("grandfathered", ()))
    lines += [f"### Most costly routes (ceilings: {ceilings['steps']:,} steps, {ceilings['statements']:,} "
              f"statements, {ceilings['bytes']:,} bytes)", ""]
    lines += _table(["Route", "Steps", "Statements", "Bytes", "Over ceiling"],
                    [[f"`{route}`", _cell(cost["steps"]), _cell(cost["statements"]), _cell(cost["bytes"]),
                      ("grandfathered" if route in grandfathered else "yes")
                      if ratchet.over_ceiling(cost, ceilings) else "no"] for route, cost in costly])
    lines += ["", f"{len(ratchet.measured(current))} routes measured; "
              f"{len(current.get('not_called', {}))} not covered.", ""]
    if current.get("not_called"):
        lines += ["<details><summary>Routes the gate cannot call</summary>", "", uncovered(current), "", "</details>", ""]
    return "\n".join(lines)


def uncovered(current: dict) -> str:
    """The routes the harness could not exercise, each with its reason."""
    rows = [[f"`{route}`", reason] for route, reason in sorted(current.get("not_called", {}).items())]
    return "\n".join(_table(["Route", "Why it is not covered"], rows))
