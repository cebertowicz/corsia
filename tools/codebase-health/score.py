#!/usr/bin/env python3
"""Composite scorer for the codebase-health harness."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

DEV_COST_MIN_PER_LOC = 30.0

REMEDIATION_MIN = {
    "cc_over_15": 30,
    "file_over_budget": 60,
    "func_over_budget": 20,
    "dup_block": 15,
    "uncovered_stmt": 1.0,
    "mypy_error": 10,
    "svelte_error": 10,
    "ruff_violation": 5,
    "bandit_med_high": 30,
    "vulture_finding": 10,
    "npm_vuln_high": 60,
    "npm_vuln_mod": 30,
    "antipattern_warn": 10,
    "antipattern_error": 30,
    "import_cycle": 120,
}

WEIGHTS = {
    "complexity":       0.1425,
    "size_budgets":     0.0950,
    "duplication":      0.0760,
    "coverage":         0.1615,
    "type_safety":      0.1140,
    "lint":             0.0760,
    "security":         0.1140,
    "dead_code":        0.0475,
    "dependencies":     0.0475,
    "architecture":     0.0760,
    "llm_suitability":  0.0500,
}

DEBT_RATIO_GRADES = [
    (0.05, "A"),
    (0.10, "B"),
    (0.20, "C"),
    (0.50, "D"),
    (1.00, "E"),
]


def debt_ratio_grade(ratio: float) -> str:
    """Map a technical-debt ratio to a letter grade (SQALE boundaries)."""
    for threshold, grade in DEBT_RATIO_GRADES:
        if ratio <= threshold:
            return grade
    return "F"


def pct_to_grade(pct: float, bounds=(90, 80, 70, 60)) -> str:
    """Map a 0-100 goodness percentage to a letter grade."""
    a, b, c, d = bounds
    if pct >= a:
        return "A"
    if pct >= b:
        return "B"
    if pct >= c:
        return "C"
    if pct >= d:
        return "D"
    return "F"


@dataclass
class DimensionScore:
    """One dimension's contribution to the composite."""

    name: str
    raw: str
    score_0_100: float
    grade: str
    remediation_min: float


def _complexity_dimension(m: dict) -> DimensionScore:
    cc_over = m["cc_over_15"]
    cc_total = m["cc_total_blocks"]
    cc_pct_over = 100 * cc_over / max(cc_total, 1)
    comp_score = max(0.0, 100 - cc_pct_over * 10)
    return DimensionScore(
        "complexity",
        f"{cc_over}/{cc_total} funcs CC>15 ({cc_pct_over:.1f}%); MI mean {m['mi_mean']:.1f}; max CC {m['cc_max']}",
        comp_score, pct_to_grade(comp_score),
        cc_over * REMEDIATION_MIN["cc_over_15"],
    )


def _size_budgets_dimension(m: dict) -> DimensionScore:
    file_viol = m["file_budget_violations"]
    func_viol = m["func_budget_violations"]
    total_files = m["total_source_files"]
    size_pct_bad = 100 * file_viol / max(total_files, 1)
    size_score = max(0.0, 100 - size_pct_bad * 5)
    return DimensionScore(
        "size_budgets",
        f"{file_viol} files over LOC budget ({size_pct_bad:.1f}%); {func_viol} funcs over 80 LOC",
        size_score, pct_to_grade(size_score),
        file_viol * REMEDIATION_MIN["file_over_budget"]
        + func_viol * REMEDIATION_MIN["func_over_budget"],
    )


def _duplication_dimension(m: dict) -> DimensionScore:
    dup_pct = m["dup_pct_weighted"]
    dup_score = max(0.0, 100 - dup_pct * 10)
    return DimensionScore(
        "duplication",
        f"{dup_pct:.2f}% weighted (py {m['dup_pct_py']:.2f}%, fe {m['dup_pct_fe']:.2f}%); {m['dup_clones']} clones",
        dup_score, pct_to_grade(dup_score),
        m["dup_clones"] * REMEDIATION_MIN["dup_block"],
    )


def _coverage_dimension(m: dict) -> DimensionScore:
    be_line = m["backend_line_cov"]
    fe_line = m["frontend_line_cov"]
    cov_blend = (be_line * m["backend_stmts"] + fe_line * m["frontend_stmts"]) / max(
        m["backend_stmts"] + m["frontend_stmts"], 1)
    cov_score = cov_blend
    return DimensionScore(
        "coverage",
        f"backend {be_line:.1f}% line / {m['backend_branch_cov']:.1f}% branch; frontend {fe_line:.1f}% line; blended {cov_blend:.1f}%",
        cov_score, pct_to_grade(cov_score, bounds=(85, 70, 55, 40)),
        m["uncovered_stmts"] * REMEDIATION_MIN["uncovered_stmt"],
    )


def _type_safety_dimension(m: dict) -> DimensionScore:
    mypy_e = m["mypy_errors"]
    svelte_e = m["svelte_errors"]
    type_total = mypy_e + svelte_e
    type_per_kloc = 1000 * type_total / max(m["total_source_loc"], 1)
    type_score = max(0.0, 100 - type_per_kloc * 20)
    return DimensionScore(
        "type_safety",
        f"mypy {mypy_e} errors (default cfg, no repo mypy gate); svelte-check {svelte_e} errors",
        type_score, pct_to_grade(type_score),
        mypy_e * REMEDIATION_MIN["mypy_error"] + svelte_e * REMEDIATION_MIN["svelte_error"],
    )


def _lint_dimension(m: dict) -> DimensionScore:
    ruff_v = m["ruff_violations"]
    ruff_per_kloc = 1000 * ruff_v / max(m["python_source_loc"], 1)
    lint_score = max(0.0, 100 - ruff_per_kloc * 15)
    return DimensionScore(
        "lint",
        f"ruff {ruff_v} violations (default ruleset, no repo ruff config); eslint not configured",
        lint_score, pct_to_grade(lint_score),
        ruff_v * REMEDIATION_MIN["ruff_violation"],
    )


def _security_dimension(m: dict) -> DimensionScore:
    bandit_mh = m["bandit_med_high_source"]
    npm_high = m["npm_vuln_high"]
    npm_mod = m["npm_vuln_mod"]
    pip_vulns = m["pip_vulns"]
    sec_penalty = bandit_mh * 0.6 + npm_high * 6 + npm_mod * 2 + pip_vulns * 8
    sec_score = max(0.0, 100 - sec_penalty)
    return DimensionScore(
        "security",
        f"bandit {bandit_mh} MED/HIGH in source (excl test-asserts); npm {npm_high} high+ / {npm_mod} mod-low; pip-audit {pip_vulns}",
        sec_score, pct_to_grade(sec_score),
        bandit_mh * REMEDIATION_MIN["bandit_med_high"]
        + npm_high * REMEDIATION_MIN["npm_vuln_high"]
        + npm_mod * REMEDIATION_MIN["npm_vuln_mod"],
    )


def _dead_code_dimension(m: dict) -> DimensionScore:
    vult = m["vulture_findings"]
    vult_per_kloc = 1000 * vult / max(m["python_source_loc"], 1)
    dead_score = max(0.0, 100 - vult_per_kloc * 30)
    return DimensionScore(
        "dead_code",
        f"vulture {vult} findings @ conf>=80 (mostly unused test vars)",
        dead_score, pct_to_grade(dead_score),
        vult * REMEDIATION_MIN["vulture_finding"],
    )


def _dependencies_dimension(m: dict) -> DimensionScore:
    npm_high = m["npm_vuln_high"]
    npm_mod = m["npm_vuln_mod"]
    pip_vulns = m["pip_vulns"]
    dep_outdated = m["npm_major_behind"] + m["pip_outdated"]
    dep_score = max(0.0, 100 - npm_high * 10 - npm_mod * 4 - pip_vulns * 10)
    return DimensionScore(
        "dependencies",
        f"pip-audit {pip_vulns} vulns / {m['pip_audited']} deps; npm {npm_high+npm_mod} vulnerable prod pkgs; {dep_outdated} outdated",
        dep_score, pct_to_grade(dep_score),
        npm_high * REMEDIATION_MIN["npm_vuln_high"] + npm_mod * REMEDIATION_MIN["npm_vuln_mod"],
    )


def _architecture_dimension(m: dict) -> DimensionScore:
    cycles = m["import_cycles"]
    ap_warn = m["antipattern_warnings"]
    ap_err = m["antipattern_errors"]
    arch_penalty = cycles * 15 + ap_err * 5 + ap_warn * 0.1
    arch_score = max(0.0, 100 - arch_penalty)
    return DimensionScore(
        "architecture",
        f"{cycles} import cycles; antipatterns {ap_err} err / {ap_warn} warn",
        arch_score, pct_to_grade(arch_score),
        cycles * REMEDIATION_MIN["import_cycle"]
        + ap_err * REMEDIATION_MIN["antipattern_error"]
        + ap_warn * REMEDIATION_MIN["antipattern_warn"],
    )


def _llm_suitability_dimension(m: dict) -> DimensionScore:
    token_fit_pct = m.get("llm_token_fit_under_8k_pct", 100.0)
    func_dig_pct  = m.get("llm_func_digestibility_pct", 100.0)
    p95_fi  = m.get("coupling_fanin_p95", 0)
    p95_fo  = m.get("coupling_fanout_p95", 0)
    coupling_score = max(0.0, 100 - (p95_fi + p95_fo) * 5)
    doc_pct = m.get("llm_docstring_coverage_pct", 0.0)
    llm_score = (token_fit_pct + func_dig_pct + coupling_score + doc_pct) / 4.0
    return DimensionScore(
        "llm_suitability",
        (
            f"token-fit (<=8k) {token_fit_pct:.1f}% of {m.get('llm_token_fit_total_files', 0)} files; "
            f"func-digestible (LOC<=80 + nest<=4) {func_dig_pct:.1f}% of "
            f"{m.get('llm_func_digestibility_total', 0)} funcs; "
            f"coupling p95 fan-in {p95_fi} / fan-out {p95_fo}; "
            f"docstring coverage {doc_pct:.1f}% of {m.get('llm_docstring_public_total', 0)} public funcs"
        ),
        llm_score, pct_to_grade(llm_score),
        0.0,
    )


def compute(metrics: dict) -> dict:
    """Compute per-dimension scores and the SQALE composite."""
    m = metrics
    dims: list[DimensionScore] = [
        _complexity_dimension(m),
        _size_budgets_dimension(m),
        _duplication_dimension(m),
        _coverage_dimension(m),
        _type_safety_dimension(m),
        _lint_dimension(m),
        _security_dimension(m),
        _dead_code_dimension(m),
        _dependencies_dimension(m),
        _architecture_dimension(m),
        _llm_suitability_dimension(m),
    ]

    composite = sum(WEIGHTS[d.name] * d.score_0_100 for d in dims)
    total_remediation_min = sum(d.remediation_min for d in dims)
    dev_cost_min = m["total_source_loc"] * DEV_COST_MIN_PER_LOC
    debt_ratio = total_remediation_min / max(dev_cost_min, 1)

    return {
        "dimensions": [d.__dict__ for d in dims],
        "weights": WEIGHTS,
        "composite_0_100": round(composite, 1),
        "composite_grade": pct_to_grade(composite),
        "remediation_minutes": round(total_remediation_min),
        "remediation_days": round(total_remediation_min / 60 / 8, 1),
        "dev_cost_minutes": round(dev_cost_min),
        "dev_cost_days": round(dev_cost_min / 60 / 8, 1),
        "technical_debt_ratio": round(debt_ratio, 4),
        "debt_ratio_pct": round(debt_ratio * 100, 2),
        "debt_ratio_grade": debt_ratio_grade(debt_ratio),
        "dev_cost_min_per_loc": DEV_COST_MIN_PER_LOC,
        "total_source_loc": m["total_source_loc"],
    }


def main() -> None:
    """Read a metrics JSON from argv[1] (or stdin), print the report JSON."""
    if len(sys.argv) > 1:
        metrics = json.load(open(sys.argv[1]))
    else:
        metrics = json.load(sys.stdin)
    print(json.dumps(compute(metrics), indent=2))


if __name__ == "__main__":
    main()
