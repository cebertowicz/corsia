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
             f"Result: **{verdict.status.replace('_', ' ')}**, {mode}. SQLite {current['sqlite_version']} "
             f"({current.get('sqlite_package', 'build not recorded')}), compared with {base_label}. The verdict uses only VM steps, statements and response bytes; "
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
    removed = [item for item in verdict.findings if item.kind == "removed"]
    if removed:
        lines += ["### Routes no longer in the application (drop them from the baseline)", ""]
        lines += _table(["Route", "Steps before"], [[f"`{item.route}`", _cell(item.before)] for item in removed])
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
    background = {route: cost["info"]["background_tasks"] for route, cost in sorted(current["routes"].items())
                  if cost.get("info", {}).get("background_tasks")}
    if background:
        lines += ["<details><summary>Background work drained inside the measured window</summary>", "",
                  "Work the application starts after its response, awaited so the read is counted with it on every run.", ""]
        lines += _table(["Route", "Tasks (name, once per start)"],
                        [[f"`{route}`", ", ".join(f"`{name}`" for name in names)] for route, names in background.items()])
        lines += ["", "</details>", ""]
    excluded = current.get("fixture_manifest", {}).get("excluded", {})
    if excluded:
        lines += [f"<details><summary>{len(excluded)} fixture tables left unseeded, each with its reason</summary>", ""]
        lines += _table(["Table", "Reason"], [[f"`{table}`", reason] for table, reason in sorted(excluded.items())])
        lines += ["", "</details>", ""]
    short = current.get("fixture_manifest", {}).get("short", {})
    if short:
        lines += [f"<details><summary>{len(short)} fixture table(s) below the census target</summary>", ""]
        lines += _table(["Table", "Target", "Rows", "Reason"],
                        [[f"`{table}`", _cell(item["target"]), _cell(item["rows"]), item["reason"]]
                         for table, item in sorted(short.items())])
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def uncovered(current: dict) -> str:
    """The routes the harness could not exercise, each with its reason."""
    rows = [[f"`{route}`", reason] for route, reason in sorted(current.get("not_called", {}).items())]
    return "\n".join(_table(["Route", "Why it is not covered"], rows))
