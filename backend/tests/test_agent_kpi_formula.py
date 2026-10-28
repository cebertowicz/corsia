"""Formula and event selection behavior for declared KPIs."""
from datetime import datetime, timezone

import pytest

from core.agent_kpi_formula import evaluate
from core.agent_kpi_operands import Event, matches_where, select


def event(name, day, **attrs):
    return Event(name, datetime(2026, 1, day, tzinfo=timezone.utc), attrs, attrs.get("subject"), attrs.get("version"), "Observed")


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_latest_and_first_use_source_time_not_arrival_order():
    rows = [event("label", 5, key="a", v="new"), event("label", 2, key="a", v="old")]
    assert select({"event": "label", "latest": ["key"]}, rows)[0].attrs["v"] == "new"
    assert select({"event": "label", "first": ["key"]}, rows)[0].attrs["v"] == "old"


@pytest.mark.parametrize("attrs,present", [({}, False), ({"id": None}, False), ({"id": ""}, False), ({"id": "a"}, True)])
def test_presence(attrs, present):
    row = event("a", 1, **attrs)
    assert matches_where(row, {"id": "present"}) is present
    assert matches_where(row, {"id": "absent"}) is not present


def test_anchor_moves_late_label_to_run_time_and_version():
    facts = [event("label", 20, run_id="a"), event("run", 3, run_id="a", version="hash")]
    rows = select({"event": "label", "match": [{"event": "run", "on": "run_id", "mode": "exists", "anchor": True}]}, facts)
    assert (rows[0].ts.day, rows[0].version) == (3, "hash")


def test_push_without_suite_does_not_match_exists():
    rows = [event("push", 1, head="a"), event("push", 2, head="b"), event("suite", 3, head="b")]
    operand = {"event": "push", "match": [{"event": "suite", "on": "head", "mode": "exists"}]}
    assert [r.attrs["head"] for r in select(operand, rows)] == ["b"]


def test_coverage_sets_count_n_and_zero_requires_owner_activity():
    review = event("review", 1, run_id="a", _declared_by_owner=True)
    spec = {"formula": "count", "num": {"event": "escape"}, "min_n": 1,
            "coverage": {"of": {"event": "review"}, "by": "run_id"}}
    row = evaluate(spec, [review], START, END)
    assert (row["value"], row["n"], row["coverage"]) == (0, 1, {"n": 1, "k": 0})
    assert evaluate(spec, [], START, END)["status"] == "too few"


def test_unknown_sum_is_partial_and_never_zero_when_every_cost_is_missing():
    spec = {"formula": "ratio", "num": {"event": "review", "sum": "cost"},
            "den": {"event": "review"}, "min_n": 1}
    row = evaluate(spec, [event("review", 1)], START, END)
    assert row["value"] is None and row["unknown"] == 1 and row["partial"]


def test_ratio_preserves_unknowns_from_sum_and_builtin_denominator():
    from core.workflow_builtin_operands import Value

    spec = {"formula": "ratio", "num": {"event": "review", "sum": "cost"},
            "den": {"builtin": "fresh_tokens"}, "min_n": 1}
    row = evaluate(spec, [event("review", 1), event("review", 2, cost=4)], START, END,
                   builtins={"fresh_tokens": Value(2, 2, 1)})
    assert row["value"] == 2 and row["unknown"] == 2 and row["partial"]


@pytest.mark.parametrize("formula,extra,expected", [
    ("count", {}, 3),
    ("ratio", {"den": {"event": "a"}}, 1),
    ("rate_per_run", {}, 3),
    ("percentile", {"num": {"event": "a", "attr": "n", "p": 50}}, 2),
    ("share", {"num": {"event": "a", "attr": "kind", "values": ["yes"]}}, 2 / 3),
])
def test_formulas(formula, extra, expected):
    facts = [event("a", i, n=i, kind="yes" if i < 3 else "no") for i in (1, 2, 3)]
    spec = {"formula": formula, "num": {"event": "a"}, "min_n": 1, **extra}
    assert evaluate(spec, facts, START, END, runs=[event("run", 1)])["value"] == expected
