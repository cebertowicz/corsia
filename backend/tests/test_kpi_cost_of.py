"""The KPI ``cost_of`` operand: the priced cost of the thread an event names."""
from __future__ import annotations

import pytest

from core.agent_kpi_formula import evaluate, unfit
from core.kpi_thread_cost import thread_costs
from core.workflow_verdict_rules import assert_evaluator_fields
from core.workflow_verdict_types import Definition
from infra.measure_validation import Lines, deciding_refusal, load, validate
from model_identity_fixtures import seed_identity_catalog, seed_manifest
from tests.test_agent_kpi_api import client, definition, fact, period, read  # noqa: F401 (client is a fixture)
from tests.test_agent_kpi_formula import END, START, event
from tests.test_measurement_api import _seed_thread

OPUS = "claude-opus-5-5"
UNLISTED = "not-a-listed-model"
LOCAL = "omlx@server/a-local-model"


def usage(db, usage_id: str, thread: str, model: str = "claude-code/opus", *, prompt: int = 1_000_000,
          manifest: str | None = "m", source: str = "external_harness:claude-code", turn: str | None = None) -> None:
    """One plan-less usage row, as a rule-started worker writes it."""
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
               "cache_read_tokens,total_tokens,source,created_at,context_manifest_id,turn_id) VALUES (?,?,?,?,0,0,0,?,"
               "?,'2026-01-02T00:00:00Z',?,?)",
               (usage_id, thread, model, prompt, prompt, source, manifest, turn))


def open_turn(db, turn_id: str, thread: str, status: str = "completed") -> None:
    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES (?,?,0,'test/model',?,'2026-01-02T00:00:00Z','test')", (turn_id, thread, status))


@pytest.fixture
def ledger(isolated_db):
    from infra.database import get_db

    db = get_db()
    for thread, user in (("w1", "steve"), ("w2", "steve"), ("w3", "steve"), ("w4", "steve"), ("w5", "steve"), ("theirs", "someone")):
        _seed_thread(db, thread, user_id=user)
    seed_identity_catalog(db)
    seed_manifest(db, "m", "claude-code", OPUS, "catalog")
    usage(db, "u1", "w1")
    usage(db, "u2a", "w2")
    usage(db, "u2b", "w2")
    usage(db, "u3", "w3", UNLISTED, manifest=None)
    usage(db, "u3b", "w3")
    usage(db, "ut", "theirs")
    usage(db, "u5", "w5", LOCAL, manifest=None, source="conversation")
    db.commit()
    try:
        yield db
    finally:
        db.close()


def test_a_priced_thread_sums_all_its_rows(ledger) -> None:
    costs = thread_costs(ledger, "steve", ["w1", "w2"])
    assert costs["w1"] > 0
    assert costs["w2"] == pytest.approx(2 * costs["w1"])


def test_one_unpriced_row_makes_the_thread_unknown_not_zero(ledger) -> None:
    assert thread_costs(ledger, "steve", ["w3"]) == {"w3": None}


def test_a_local_only_thread_costs_zero_not_unknown(ledger) -> None:
    assert thread_costs(ledger, "steve", ["w5"]) == {"w5": 0.0}


def test_a_thread_with_no_usage_or_another_users_thread_is_absent(ledger) -> None:
    assert thread_costs(ledger, "steve", ["w4", "theirs", "missing"]) == {}
    assert thread_costs(ledger, "steve", []) == {}


def test_every_thread_is_read_in_one_query(ledger) -> None:
    open_turn(ledger, "settled", "w1")
    queries: list[str] = []
    ledger.set_trace_callback(queries.append)
    thread_costs(ledger, "steve", ["w1", "w2", "w3", "w4"])
    ledger.set_trace_callback(None)
    assert sum("FROM llm_usage" in query and "LIMIT 0" not in query for query in queries) == 1  # LIMIT 0 only prepares


def _ratio(num: dict) -> dict:
    return {"formula": "ratio", "min_n": 1, "num": {"event": "qa.done", **num}, "den": {"event": "qa.done", "distinct": "pr"}}


COSTS = {"w1": 1.5, "w2": 2.5, "w3": None}


def test_ratio_is_the_known_cost_over_distinct_prs() -> None:
    facts = [event("qa.done", 5, pr="1", worker="w1"), event("qa.done", 6, pr="2", worker="w2")]
    result = evaluate(_ratio({"cost_of": "worker"}), facts, START, END, thread_costs=COSTS)
    assert (result["value"], result["n"], result["unknown"]) == (2.0, 2, 0)


@pytest.mark.parametrize("row", [
    {"pr": "2", "worker": "w3"},   # a thread with an unpriced row
    {"pr": "2"},                   # no attribute
    {"pr": "2", "worker": ""},     # an empty attribute
    {"pr": "2", "worker": "w4"},   # a thread with no usage
])
def test_an_event_whose_cost_is_unknown_counts_unknown_and_adds_nothing(row) -> None:
    facts = [event("qa.done", 5, pr="1", worker="w1"), event("qa.done", 6, **row)]
    result = evaluate(_ratio({"cost_of": "worker"}), facts, START, END, thread_costs=COSTS)
    assert (result["value"], result["n"], result["unknown"], result["partial"]) == (0.75, 2, 1, True)


def test_every_event_unknown_is_no_value_not_zero() -> None:
    facts = [event("qa.done", 5, pr="1"), event("qa.done", 6, pr="2", worker="w3")]
    result = evaluate(_ratio({"cost_of": "worker"}), facts, START, END, thread_costs=COSTS)
    assert (result["value"], result["unknown"]) == (None, 2)


def test_count_formula_totals_the_cost() -> None:
    spec = {"formula": "count", "min_n": 1, "num": {"event": "qa.done", "cost_of": "worker"}}
    facts = [event("qa.done", 5, worker="w1"), event("qa.done", 6, worker="w2")]
    assert evaluate(spec, facts, START, END, thread_costs=COSTS)["value"] == 4.0


def test_the_agent_kpi_read_prices_the_threads_for_all_periods_at_once(client, ledger) -> None:  # noqa: F811
    http, path = client
    definition(ledger, path, "event", "demo.review", {"attributes": {}})
    definition(ledger, path, "kpi", "cost", {"formula": "ratio", "min_n": 1,
               "num": {"event": "demo.review", "cost_of": "worker"}, "den": {"event": "demo.review", "distinct": "pr"}})
    for day, (pr, worker) in zip((5, 6, 7), (("1", "w1"), ("2", "w2"), ("3", "w3"))):
        fact(ledger, "demo.review", day, {"pr": pr, "worker": worker})
    ledger.commit()
    costs = thread_costs(ledger, "steve", ["w1", "w2"])
    row = period(read(http), "cost")
    assert row["value"] == pytest.approx((costs["w1"] + costs["w2"]) / 3)
    assert (row["n"], row["unknown"], row["partial"]) == (3, 1, True)


GOOD = """schema: corsia.measures/1
namespace: demo
events:
  demo.review:
    attributes: {pr: id, worker: id}
    dedupe: [pr]
kpis:
  cost:
    title: Cost
    unit: usd
    better: lower
    formula: %s
    num: %s
    den: %s
    min_n: 1
"""


def _declare(formula="ratio", num="{event: demo.review, cost_of: worker}", den="{event: demo.review, distinct: pr}"):
    text = GOOD % (formula, num, den)
    return validate(load(text), Lines(text))


@pytest.mark.parametrize("formula", ["ratio", "count"])
def test_validation_accepts_cost_of_on_num_for_ratio_and_count(formula) -> None:
    _declare(formula)


@pytest.mark.parametrize("formula,num,den,reason", [
    ("ratio", "{event: demo.review}", "{event: demo.review, cost_of: worker}", "only on num, not den"),
    ("share", "{event: demo.review, cost_of: worker, attr: worker, values: [a]}", "{event: demo.review}", "formula ratio or count"),
    ("ratio", "{event: demo.review, cost_of: worker, sum: n}", "{event: demo.review}", "combined with sum or builtin"),
    ("ratio", "{builtin: cost_usd, cost_of: worker}", "{event: demo.review}", "combined with sum or builtin"),
    ("ratio", "{event: demo.review, cost_of: ''}", "{event: demo.review}", "name of the event attribute"),
])
def test_validation_refuses_cost_of_where_it_cannot_apply(formula, num, den, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        _declare(formula, num, den)


def test_a_stored_spec_with_a_misplaced_cost_of_is_not_tracked_and_not_computed() -> None:
    spec = _ratio({}) | {"den": {"event": "qa.done", "cost_of": "worker"}}
    assert "only on num" in unfit(spec)
    result = evaluate(spec, [event("qa.done", 5, worker="w1")], START, END, thread_costs=COSTS)
    assert result["status"] == "not tracked" and "only on num" in result["reason"]


def test_the_verdict_read_accepts_a_deciding_cost_kpi_and_refuses_a_misplaced_one() -> None:
    events = {"qa.done": {"attributes": {"pr": "id", "worker": "id"}}}
    kpi = {"formula": "ratio", "num": {"event": "qa.done", "cost_of": "worker"}, "den": {"event": "qa.done", "distinct": "pr"},
           "min_n": 1, "can_decide": True, "decides_as": "spend", "better": "lower", "target": 1, "review": 2,
           "coverage": {"of": "qa.done", "by": "pr"}}
    assert deciding_refusal("cost", kpi, events, "review") is None
    bad = {**kpi, "den": {"event": "qa.done", "cost_of": "worker"}}
    assert "only on num" in (deciding_refusal("cost", bad, events, "review") or "")
    definitions = [Definition("event", "qa.done", 1, events["qa.done"], "2026-01-01T00:00:00Z", False, "f"),
                   Definition("kpi", "cost", 1, kpi, "2026-01-01T00:00:00Z", False, "f"),
                   Definition("kpi", "bad", 1, bad, "2026-01-01T00:00:00Z", False, "f")]
    measures, refusals, _ = assert_evaluator_fields(definitions, "review")
    assert [m.name for m in measures] == ["cost"] and [name for name, _ in refusals] == ["bad"]
