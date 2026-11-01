"""Evaluate declared KPI formulas over selected event rows."""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Sequence

from core.agent_kpi_operands import Event, attribute, distinct, select

_LABEL_ORDER = {"Reported by another agent": 0, "Self-reported": 1, "Imported": 2,
                "Corroborated": 3, "Observed": 4}


def _in_bucket(rows: Sequence[Event], start: datetime, end: datetime, version: str | None) -> list[Event]:
    return [row for row in rows if start <= row.ts < end and (version is None or (row.version or "") == version)]


def _coverage(spec: Mapping[str, Any], numerator: Sequence[Event], selected: Mapping[str, Sequence[Event]],
              start: datetime, end: datetime, version: str | None) -> dict[str, int] | None:
    coverage = spec.get("coverage")
    if not coverage:
        return None
    key = coverage["by"]
    universe = _in_bucket(selected["coverage_of"], start, end, version)
    labels = _in_bucket(selected["coverage_labelled"], start, end, version) if coverage.get("labelled") else numerator
    eligible = {attribute(row, key) for row in universe} - {None, ""}
    labelled = {attribute(row, key) for row in labels} - {None, ""}
    return {"n": len(eligible), "k": len(eligible & labelled)}


def _labels(rows: Sequence[Event]) -> tuple[str | None, dict[str, int]]:
    mix = dict(Counter(row.label for row in rows))
    weakest = min(mix, key=lambda label: _LABEL_ORDER.get(label, 0)) if mix else None
    return weakest, mix


def prepare(spec: Mapping[str, Any], facts: Sequence[Event]) -> dict[str, list[Event]]:
    """Select each operand once for all periods and versions."""
    operands = {"num": spec["num"], "den": spec.get("den", {})}
    coverage = spec.get("coverage") or {}
    operands.update({"coverage_of": coverage.get("of", {}), "coverage_labelled": coverage.get("labelled", {})})
    return {name: select(operand, facts) for name, operand in operands.items()}


def evaluate(spec: Mapping[str, Any], facts: Sequence[Event], start: datetime, end: datetime,
             version: str | None = None, builtins: Mapping[str, Any] | None = None,
             selected: Mapping[str, Sequence[Event]] | None = None) -> dict[str, Any]:
    """Compute one KPI in a period and optional version; None selects all versions."""
    num_spec = spec["num"]
    selected = selected if selected is not None else prepare(spec, facts)
    builtins = builtins or {}
    needed = [op['builtin'] for op in (num_spec, spec.get('den', {})) if 'builtin' in op]
    if spec['formula'] == 'rate_per_run' and 'runs' in builtins:
        needed.append('runs')
    reason = next((builtins[name].reason for name in needed if builtins[name].reason), None)
    if reason:
        return {'value': None, 'n': 0, 'coverage': None, 'label': None, 'label_mix': {},
                'unknown': 0, 'status': 'not tracked', 'reason': reason, 'partial': False}
    raw_num = _in_bucket(selected["num"], start, end, version)
    numerator = distinct(raw_num, num_spec.get("distinct"))
    formula = spec["formula"]
    coverage = _coverage(spec, raw_num, selected, start, end, version)
    num_builtin = builtins.get(num_spec.get('builtin'))
    n = num_builtin.n if num_builtin else len(numerator)
    value: float | int | None = None
    unknown = num_builtin.unknown if num_builtin else 0
    if formula == "count":
        value = num_builtin.value if num_builtin else len(numerator)
        if coverage:
            n = coverage["n"]
    elif formula == "ratio":
        den_spec = spec["den"]
        den_builtin = builtins.get(den_spec.get('builtin'))
        denominator = distinct(_in_bucket(selected["den"], start, end, version), den_spec.get("distinct"))
        n = den_builtin.n if den_builtin else len(denominator)
        divisor = den_builtin.value if den_builtin else len(denominator)
        if den_builtin:
            unknown += den_builtin.unknown
        total = num_builtin.value if num_builtin else float(len(numerator))
        if "sum" in num_spec:
            values = [attribute(row, num_spec["sum"]) for row in numerator]
            num_unknown = sum(value is None for value in values)
            unknown += num_unknown
            total = sum(value for value in values if value is not None) if num_unknown < len(values) else None
        value = total / divisor if divisor and total is not None else None
    elif formula == "rate_per_run":
        run_builtin = builtins.get('runs')
        n = run_builtin.value if run_builtin else 0
        total = num_builtin.value if num_builtin else len(numerator)
        if run_builtin:
            unknown += run_builtin.unknown
        value = total / n if n and total is not None else None
    elif formula == "share":
        values = num_spec["values"]
        value = sum(attribute(row, num_spec["attr"]) in values for row in numerator) / n if n else None
    elif formula == "percentile":
        values = sorted(attribute(row, num_spec["attr"]) for row in numerator if attribute(row, num_spec["attr"]) is not None)
        unknown = n - len(values)
        value = values[max(0, math.ceil(num_spec["p"] / 100 * len(values)) - 1)] if values else None
    label, label_mix = _labels(numerator)
    if num_builtin:
        label, label_mix = 'Observed', {'Observed': num_builtin.n}
    any_activity = any(start <= row.ts < end and (version is None or (row.version or "") == version)
                       and row.attrs.get("_declared_by_owner", False) for row in facts)
    no_activity = formula == "count" and not num_builtin and not numerator and not any_activity
    too_few = n < spec["min_n"] or no_activity
    # A count past its line wakes even below min_n (its n counts a base, not the rows being counted).
    count = value if formula == "count" and not no_activity else None
    return {"value": None if too_few else value, "count": count, "n": n, "coverage": coverage,
            "label": label, "label_mix": label_mix, "unknown": unknown,
            "status": "too few" if too_few else "ok", "partial": bool(unknown)}
