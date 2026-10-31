"""Formula and event selection behavior for declared KPIs."""
from datetime import datetime, timezone

import pytest

from core.agent_kpi_formula import evaluate
from core.agent_kpi_operands import Event, matches_where, select
from core.workflow_builtin_operands import Value


def event(name, day, **attrs):
    return Event(name, datetime(2026, 1, day, tzinfo=timezone.utc), attrs, attrs.get("subject"), attrs.get("version"), "Observed")


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_latest_and_first_use_source_time_not_arrival_order():
    rows = [event("label", 5, key="a", v="new"), event("label", 2, key="a", v="old")]
    assert select({"event": "label", "latest": ["key"]}, rows)[0].attrs["v"] == "new"
    assert select({"event": "label", "first": ["key"]}, rows)[0].attrs["v"] == "old"


def test_latest_counts_a_relabelled_item_only_under_its_current_value():
    rows = [event("label", 2, key="a", cause="unknown"), event("label", 5, key="a", cause="missed-test"),
            event("label", 3, key="b", cause="unknown")]
    assert [row.attrs["key"] for row in select({"event": "label", "where": {"cause": "unknown"}, "latest": ["key"]}, rows)] == ["b"]
    assert [row.attrs["key"] for row in select({"event": "label", "where": {"cause": "missed-test"}, "latest": ["key"]}, rows)] == ["a"]


def test_first_reads_the_earliest_row_that_qualifies():
    rows = [event("comment", 2, key="a", reviewer="no"), event("comment", 5, key="a", reviewer="yes", v="review")]
    assert select({"event": "comment", "where": {"reviewer": "yes"}, "first": ["key"]}, rows)[0].attrs["v"] == "review"


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


@pytest.mark.parametrize('mode', ['exists', 'absent', 'exactly_one'])
def test_match_agrees_with_scan_including_anchor_and_missing_keys(mode):
    from dataclasses import replace
    facts = [event('label', 10, key='a'), event('label', 11, key='b'), event('label', 12),
             event('run', 1, key='a', version='old'), event('run', 3, key='a', version='new'),
             event('run', 2, key='b', version='only')]
    clause = {'event': 'run', 'on': 'key', 'mode': mode, 'anchor': True}
    expected = []
    for row in facts[:3]:
        value = row.attrs.get('key')
        matches = [r for r in facts if value is not None and r.name == 'run' and r.attrs.get('key') == value]
        if (mode == 'exists' and not matches) or (mode == 'absent' and matches) or (mode == 'exactly_one' and len(matches) != 1):
            continue
        if matches:
            anchor = max(matches, key=lambda r: r.ts)
            row = replace(row, ts=anchor.ts, version=anchor.version)
        expected.append(row)
    assert select({'event': 'label', 'match': [clause]}, facts) == expected


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
    assert evaluate(spec, facts, START, END, builtins={"runs": Value(1, 1)})["value"] == expected
