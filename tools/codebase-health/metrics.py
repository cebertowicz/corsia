#!/usr/bin/env python3
"""Automated metric collector for the codebase-health harness.

Runs the deterministic, repo-dep-free instruments (radon, ruff, mypy, bandit,
jscpd, vulture, grimp) against a worktree and emits the bulk of the metrics
JSON consumed by score.py. The three instruments that need the repo's own
runtime venv + workspace sentinel — pytest coverage, vitest coverage,
svelte-check — are run separately (see METHODOLOGY.md) and their numbers are
merged into the metrics JSON before scoring.

Run from the harness (run.sh) or directly:
    python tools/codebase-health/metrics.py --worktree <wt> --out <dir> --venv <venv>
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
from collections import Counter

from llm_metrics import llm_suitability
from loc_ledger import loc_ledger
from metric_utils import (
    FUNC_LIMIT,
    PY_FILE_LIMIT,
    SVELTE_FILE_LIMIT,
    TS_FILE_LIMIT,
    is_test,
    loc,
    walk_files,
)


def size_budgets(wt: str) -> dict:
    """Count file-size and function-length budget violations."""
    os.chdir(wt)
    py = list(walk_files(["backend", "tools"], (".py",)))
    sv = list(walk_files(["frontend/src"], (".svelte",)))
    ts = [p for p in walk_files(["frontend/src"], (".ts",)) if not p.endswith(".d.ts")]

    file_viol = (
        sum(1 for p in py if loc(p) > PY_FILE_LIMIT)
        + sum(1 for p in sv if loc(p) > SVELTE_FILE_LIMIT)
        + sum(1 for p in ts if loc(p) > TS_FILE_LIMIT)
    )
    func_viol = 0
    for p in py:
        try:
            tree = ast.parse(open(p, encoding="utf-8", errors="replace").read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.end_lineno and node.end_lineno - node.lineno + 1 > FUNC_LIMIT:
                    func_viol += 1

    src_py = [p for p in py if not is_test(p)]
    src_fe = [p for p in (sv + ts) if not is_test(p)]
    return {
        "file_budget_violations": file_viol,
        "func_budget_violations": func_viol,
        "total_source_files": len(py) + len(sv) + len(ts),
        "python_source_loc": sum(loc(p) for p in src_py),
        "frontend_source_loc": sum(loc(p) for p in src_fe),
        "total_source_loc": sum(loc(p) for p in src_py + src_fe),
    }


def complexity(out: str) -> dict:
    """Parse radon CC JSON for over-15 count, max, total blocks."""
    cc = json.load(open(os.path.join(out, "cc.json")))
    blocks = [it for items in cc.values() if isinstance(items, list) for it in items]
    cs = [b["complexity"] for b in blocks if b.get("complexity") is not None]
    return {
        "cc_total_blocks": len(cs),
        "cc_over_15": sum(1 for c in cs if c > 15),
        "cc_max": max(cs) if cs else 0,
    }


def maintainability(out: str) -> dict:
    """Parse radon MI JSON for mean maintainability index."""
    mi = json.load(open(os.path.join(out, "mi.json")))
    vals = [v["mi"] for v in mi.values() if isinstance(v, dict) and "mi" in v]
    return {"mi_mean": round(sum(vals) / max(len(vals), 1), 1)}


def ruff_lint(venv: str) -> dict:
    """Run ruff (default ruleset) and count violations."""
    p = subprocess.run(
        [f"{venv}/bin/ruff", "check", "backend", "tools", "--output-format=json"],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        data = []
    return {"ruff_violations": len(data)}


def mypy_types(venv: str) -> dict:
    """Run mypy on non-test backend (default cfg, ignore-missing-imports)."""
    p = subprocess.run(
        [f"{venv}/bin/mypy", "backend", "--ignore-missing-imports",
         "--exclude", r"(/__pycache__/|/\.venv|/tests/)", "--no-error-summary"],
        capture_output=True, text=True,
    )
    errs = sum(1 for ln in p.stdout.splitlines() if ": error:" in ln)
    return {"mypy_errors": errs}


def bandit_security(venv: str) -> dict:
    """Run bandit; count MED/HIGH findings in source (excl test-asserts)."""
    p = subprocess.run(
        [f"{venv}/bin/bandit", "-r", "backend", "tools", "-f", "json", "-q"],
        capture_output=True, text=True,
    )
    try:
        res = json.loads(p.stdout or "{}").get("results", [])
    except json.JSONDecodeError:
        res = []
    mh = sum(
        1 for r in res
        if r["test_id"] != "B101"
        and not is_test(r["filename"])
        and r["issue_severity"] in ("MEDIUM", "HIGH")
    )
    return {"bandit_med_high_source": mh}


def vulture_dead(venv: str) -> dict:
    """Run vulture at min-confidence 80; count findings."""
    p = subprocess.run(
        [f"{venv}/bin/vulture", "backend", "tools", "--min-confidence", "80"],
        capture_output=True, text=True,
    )
    return {"vulture_findings": sum(1 for ln in p.stdout.splitlines() if ":" in ln)}


def import_cycles(venv: str) -> dict:
    """Detect backend import cycles via grimp + networkx SCC."""
    code = (
        "import grimp,networkx as nx,json;"
        "g=grimp.build_graph('backend');G=nx.DiGraph();"
        "[G.add_edge(m,i) for m in g.modules for i in g.find_modules_directly_imported_by(m)];"
        "print(len([c for c in nx.strongly_connected_components(G) if len(c)>1]))"
    )
    p = subprocess.run([f"{venv}/bin/python", "-c", code], capture_output=True, text=True)
    try:
        n = int(p.stdout.strip() or "0")
    except ValueError:
        n = 0
    return {"import_cycles": n}


def antipatterns(out: str) -> dict:
    """Bucket the repo's own antipattern suite output by severity."""
    path = os.path.join(out, "antipatterns.txt")
    if not os.path.exists(path):
        return {"antipattern_warnings": 0, "antipattern_errors": 0, "antipattern_by_rule": {}}
    lines = open(path).read().splitlines()
    rule = Counter()
    warn = err = 0
    for ln in lines:
        if ln.startswith("WARNING"):
            warn += 1
            parts = ln.split()
            if len(parts) > 1:
                rule[parts[1]] += 1
        elif ln.startswith("ERROR"):
            err += 1
    return {"antipattern_warnings": warn, "antipattern_errors": err,
            "antipattern_by_rule": dict(rule)}


def main() -> None:
    """Collect all repo-dep-free metrics into <out>/metrics-partial.json."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--venv", required=True)
    args = ap.parse_args()

    metrics: dict = {}
    metrics.update(size_budgets(args.worktree))
    metrics.update(complexity(args.out))
    metrics.update(maintainability(args.out))
    metrics.update(ruff_lint(args.venv))
    metrics.update(mypy_types(args.venv))
    metrics.update(bandit_security(args.venv))
    metrics.update(vulture_dead(args.venv))
    metrics.update(import_cycles(args.venv))
    metrics.update(antipatterns(args.out))
    metrics.update(llm_suitability(args.worktree, args.venv))
    metrics.update(loc_ledger(args.worktree))

    path = os.path.join(args.out, "metrics-partial.json")
    json.dump(metrics, open(path, "w"), indent=2)
    print(f"wrote {path}")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
