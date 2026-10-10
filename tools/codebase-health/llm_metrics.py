"""Local-LLM suitability metrics for the codebase-health harness."""

from __future__ import annotations

import ast
import json
import os
import subprocess

from metric_utils import (
    CHARS_PER_TOKEN,
    FUNC_LIMIT,
    NEST_LIMIT,
    TOKEN_HEADLINE_LIMIT,
    is_test,
    max_nesting,
    walk_files,
)


def llm_suitability(wt: str, venv: str) -> dict:
    os.chdir(wt)
    all_src = (
        list(walk_files(["backend", "tools"], (".py",)))
        + list(walk_files(["frontend/src"], (".svelte",)))
        + [
            p
            for p in walk_files(["frontend/src"], (".ts",))
            if not p.endswith(".d.ts")
        ]
    )
    tok_counts = [_token_estimate(p) for p in all_src]

    func_digest = _function_digestibility()
    coupling = _coupling_stats(venv)
    docstrings = _docstring_coverage()

    return {
        "llm_token_fit_under_2k_pct": _pct(sum(1 for t in tok_counts if t <= 2000), len(all_src)),
        "llm_token_fit_under_4k_pct": _pct(sum(1 for t in tok_counts if t <= 4000), len(all_src)),
        "llm_token_fit_under_8k_pct": _pct(
            sum(1 for t in tok_counts if t <= TOKEN_HEADLINE_LIMIT),
            len(all_src),
        ),
        "llm_token_fit_total_files": len(all_src),
        **func_digest,
        **docstrings,
        **coupling,
    }


def _token_estimate(path: str) -> float:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return len(handle.read()) / CHARS_PER_TOKEN
    except OSError:
        return 0.0


def _function_digestibility() -> dict:
    total_funcs = 0
    both_ok = 0
    for path in _source_python():
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total_funcs += 1
                lines = (node.end_lineno or node.lineno) - node.lineno + 1
                if lines <= FUNC_LIMIT and max_nesting(node) <= NEST_LIMIT:
                    both_ok += 1
    return {
        "llm_func_digestibility_pct": _pct(both_ok, total_funcs),
        "llm_func_digestibility_total": total_funcs,
    }


def _coupling_stats(venv: str) -> dict:
    code = (
        "import grimp,statistics as S,json;"
        "g=grimp.build_graph('backend');"
        "mods=list(g.modules);"
        "fi=[len(g.find_modules_that_directly_import(m)) for m in mods];"
        "fo=[len(g.find_modules_directly_imported_by(m)) for m in mods];"
        "fi_s=sorted(fi);fo_s=sorted(fo);"
        "n=len(mods);"
        "print(json.dumps({"
        "'coupling_fanin_median':S.median(fi),'coupling_fanin_p95':fi_s[int(0.95*n)],"
        "'coupling_fanout_median':S.median(fo),'coupling_fanout_p95':fo_s[int(0.95*n)],"
        "'coupling_modules':n}))"
    )
    proc = subprocess.run(
        [f"{venv}/bin/python", "-c", code],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {
            "coupling_fanin_median": 0,
            "coupling_fanin_p95": 0,
            "coupling_fanout_median": 0,
            "coupling_fanout_p95": 0,
            "coupling_modules": 0,
        }


def _docstring_coverage() -> dict:
    total_public = 0
    with_doc = 0
    for path in _source_python():
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            total_public += 1
            if ast.get_docstring(node) is not None:
                with_doc += 1
    return {
        "llm_docstring_coverage_pct": _pct(with_doc, total_public),
        "llm_docstring_public_total": total_public,
    }


def _source_python() -> list[str]:
    return [p for p in walk_files(["backend", "tools"], (".py",)) if not is_test(p)]


def _pct(count: int, total: int) -> float:
    return round(100 * count / max(total, 1), 1)
