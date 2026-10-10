"""Models rows: identity and price from Cost's owners, accepted tasks, nothing dropped."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api import measurement_model_cost, measurement_models
from api.cost_identity import IdentityResolver, ModelIdentity
from api.measurement_model_cost import query_model_costs
from api.measurement_models import build_models, models_payload
from api.measurement_scope import resolve_grain
from model_identity_fixtures import (
    ALIBABA_HOST, LOCAL_ID, LOCAL_MODEL, seed_external_server, seed_identity_catalog, seed_manifest,
)

# Weeks grain in UTC as of Monday 2026-10-05: the latest window is the four completed weeks
# 09-07..09-28 and the previous one 08-10..08-31. The ``_build`` helpers below name one period of each.
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
LATEST, PREVIOUS = "2026-09-28", "2026-09-21"
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "steve"}
RECEIPT = "0123456789abcdef0123456{}"  # a 24-hex Codex receipt id proves the token contract


def _identity(key: str, label: str | None = None, resolved: bool = True) -> ModelIdentity:
    return ModelIdentity(key, label or key, resolved)


def _resolve(row) -> ModelIdentity:
    return _identity(row["model"], resolved=not str(row["model"]).startswith("alias/"))


def _cost(key: str, task: str, outcome: str = "priced", dollars: float | None = 1.0, period: str = LATEST) -> dict:
    return {"period": period, "task_id": task, "identity": _identity(key), "outcome": outcome, "dollars": dollars}


def _build(tokens=(), turns=(), errors=(), costs=(), offered=frozenset(), gaps=()):
    return build_models(list(tokens), list(turns), list(errors), list(costs), resolve=_resolve,
                        latest=(LATEST,), previous=(PREVIOUS,), offered=set(offered), **({"gaps": gaps} if gaps else {}))


def test_models_under_fourteen_tasks_fold_into_other_with_unknown_cost_kept():
    costs = [_cost("big", f"b{index}") for index in range(14)]
    costs += [_cost("small-a", "s1"), _cost("small-b", "s2", outcome="no_list_price", dollars=None)]
    built = _build(turns=[{"model": "small-a", "turns": 30}, {"model": "small-b", "turns": 30}],
                   errors=[{"model": "small-a", "error": "socket closed"}], costs=costs, offered={"big"})
    big, other = built["rows"]
    assert (big["model"], big["tasks"], big["recorded_cost_per_task"], big["cost_class"]) == ("big", 14, 1.0, "priced")
    assert other["other"] is True and other["label"] == "Other models (under 14 tasks)"
    assert (other["tasks"], other["failed_turns_per_100"]) == (2, pytest.approx(100 / 60))
    # Other keeps small-a's priced dollar over both tasks and stays marked partial.
    assert (other["recorded_cost_per_task"], other["cost_class"]) == (0.5, "no_pricing")


def test_a_few_unpriced_calls_keep_the_known_rate_and_mark_it_partial():
    """Like Cost: show the priced amount and mark it incomplete, never withhold it."""
    tasks = [f"t{index}" for index in range(360)]
    costs = [_cost("opus", tasks[index % 360], dollars=0.06) for index in range(15_000)]
    costs += [_cost("opus", tasks[index], outcome="tokens_unreported", dollars=None) for index in range(26)]
    costs += [_cost("opus", tasks[index % 20], dollars=0.05, period=PREVIOUS) for index in range(200)]
    costs += [_cost("opus", "p0", outcome="contract_unproven", dollars=None, period=PREVIOUS)]
    (row,) = _build(costs=costs)["rows"]
    assert (row["tasks"], row["cost_class"]) == (360, "no_pricing")
    assert row["recorded_cost_per_task"] == pytest.approx(15_000 * 0.06 / 360)
    # The previous period is partial too: its unpriced task p0 still counts as a task.
    assert row["cost_change"] == pytest.approx((15_000 * 0.06 / 360) / (200 * 0.05 / 21) - 1)


def _period(key: str, count: int, period: str = LATEST, **call) -> list[dict]:
    return [_cost(key, f"{key}-{period}-{index}", period=period, **call) for index in range(count)]


def _reasons(costs=(), tokens=()) -> dict[str, tuple]:
    return {row["model"]: (row["rate_reason"], row["change_reason"], row["cost_change"])
            for row in _build(costs=costs, tokens=tokens)["rows"]}


def test_a_change_is_a_percentage_against_a_previous_period_at_the_task_floor():
    costs = _period("m", 14, dollars=2.0) + _period("m", 14, PREVIOUS, dollars=1.0)
    assert _reasons(costs)["m"] == ("none", "none", pytest.approx(1.0))


def test_a_previous_period_under_the_task_floor_is_too_few_before_not_a_percentage():
    costs = _period("m", 14, dollars=2.0) + _period("m", 13, PREVIOUS, dollars=1.0)
    assert _reasons(costs)["m"] == ("none", "too_few_before", None)


def test_no_previous_tasks_is_new():
    assert _reasons(_period("m", 14, dollars=2.0))["m"] == ("none", "new", None)


def test_a_zero_dollar_previous_rate_is_a_rate_with_no_percentage_never_unpriced():
    costs = _period("m", 14, dollars=2.0) + _period("m", 14, PREVIOUS, dollars=0.0)
    assert _reasons(costs)["m"] == ("none", "none", None)


def test_no_price_on_either_side_is_unpriced():
    unpriced = {"outcome": "contract_unproven", "dollars": None}
    costs = _period("now", 14, **unpriced) + _period("now", 14, PREVIOUS, dollars=1.0)
    costs += _period("then", 14, dollars=1.0) + _period("then", 14, PREVIOUS, **unpriced)
    reasons = _reasons(costs)
    assert reasons["now"] == ("unpriced", "unpriced", None)
    assert reasons["then"] == ("none", "unpriced", None)


def test_a_local_model_has_no_list_price_in_both_cells():
    local = {"outcome": "not_api_priced", "dollars": None}
    assert _reasons(_period("m", 14, **local))["m"] == ("local", "local", None)


def test_a_row_with_no_tasks_this_period_names_no_cause():
    (other,) = _build(tokens=[{"model": "token-only", "tokens": 10}])["rows"]
    assert (other["tasks"], other["rate_reason"], other["change_reason"], other["cost_change"]) == (0, "no_tasks", "none", None)


def test_a_task_using_two_folded_models_counts_once_in_the_floor_row():
    costs = [_cost("big", f"b{index}") for index in range(14)]
    costs += [_cost("small-a", "shared"), _cost("small-b", "shared"), _cost("small-b", "own")]
    costs += [_cost("small-a", "was", period=PREVIOUS), _cost("small-b", "was", period=PREVIOUS)]
    other = _build(costs=costs)["rows"][-1]
    # Two distinct tasks, not three per-model counts; the rate divides by them.
    assert (other["tasks"], other["recorded_cost_per_task"], other["previous_tasks"]) == (2, 1.5, 1)


def test_priced_plus_local_floor_row_is_priced_and_leaves_local_tasks_out_of_the_rate():
    costs = [_cost("small-a", "p1", dollars=2.0), _cost("local", "l1", outcome="not_api_priced", dollars=None)]
    (other,) = _build(costs=costs)["rows"]
    assert (other["cost_class"], other["includes_local"]) == ("priced", True)
    assert (other["tasks"], other["recorded_cost_per_task"]) == (2, 2.0)


def test_the_minimum_tasks_floor_is_served():
    assert _build()["min_tasks"] == 14


def test_a_recording_gap_marks_the_rate_possibly_low():
    costs = [_cost("big", f"b{index}") for index in range(14)] + [_cost("other", "o1")]
    rows = _build(costs=costs, gaps={"b3"})["rows"]
    assert [row["recording_gap"] for row in rows] == [True, False]


def test_token_shares_past_six_share_one_counted_other_slice():
    shares = _build(tokens=[{"model": f"m{index}", "tokens": 100 - index} for index in range(9)])["token_shares"]
    assert [row["model"] for row in shares] == [f"m{index}" for index in range(6)] + [""]
    assert shares[-1]["label"] == "Other (3 more models)"
    assert shares[-1]["share_of_tokens"] == pytest.approx((94 + 93 + 92) / sum(100 - index for index in range(9)))
    assert (shares[0]["tokens"], shares[-1]["tokens"]) == (100, 94 + 93 + 92)


def test_spend_is_the_priced_dollars_and_none_when_nothing_is_priced():
    costs = [_cost("big", f"b{index}", dollars=2.0) for index in range(14)]
    costs += [_cost("ext", f"e{index}", outcome="contract_unproven", dollars=None) for index in range(14)]
    rows = {row["model"]: row for row in _build(costs=costs)["rows"]}
    assert (rows["big"]["spend_usd"], rows["ext"]["spend_usd"]) == (28.0, None)


def test_rate_is_unknown_only_when_nothing_is_priced():
    costs = [_cost("ext", f"t{index}", outcome="contract_unproven", dollars=None) for index in range(14)]
    costs += [_cost("local", f"l{index}", outcome="not_api_priced", dollars=None) for index in range(14)]
    rows = {row["model"]: row for row in _build(costs=costs)["rows"]}
    assert (rows["ext"]["recorded_cost_per_task"], rows["ext"]["cost_class"]) == (None, "no_pricing")
    assert (rows["local"]["recorded_cost_per_task"], rows["local"]["cost_class"]) == (None, "local")


def test_unmatched_failed_turns_count_only_failures():
    built = _build(errors=[{"model": "alias/opus", "error": "socket closed"},
                           {"model": "alias/opus", "error": "usage limit reached"}])
    assert built["unmatched_failed_turns"] == 1


def test_rows_are_keyed_by_full_identity_even_when_labels_match():
    costs = [_cost(key, f"{key}-{index}") for key in ("codex/gpt-5-codex", "openai/gpt-5-codex") for index in range(14)]
    built = build_models([], [], [], [{**row, "identity": _identity(row["identity"].key, "gpt-5-codex")} for row in costs],
                         resolve=_resolve, latest=(LATEST,), previous=(PREVIOUS,), offered={"codex/gpt-5-codex"})
    offered = {row["model"]: row["not_offered_now"] for row in built["rows"]}
    assert offered == {"codex/gpt-5-codex": False, "openai/gpt-5-codex": True}


def test_unresolved_share_keeps_the_alias_as_its_own_slice():
    built = _build(tokens=[{"model": "known", "tokens": 90}, {"model": "alias/opus", "tokens": 10}])
    assert [row["model"] for row in built["token_shares"]] == ["known", "alias/opus"]
    assert built["unresolved_token_share"] == pytest.approx(0.1)


# --- read path: accepted tasks, Cost admission, catalog identity -----------------


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES ('root','steve','root',1,'active','t','t')")
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','root','active',1,'t','t')")
    seed_identity_catalog(connection)
    try:
        yield connection
    finally:
        connection.close()


def _task(db, node: str, accepted_at: str | None = "2026-09-30T12:00:00Z") -> None:
    db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES (?, 'plan',NULL,1,'task','task','done',?,?,1,1,'t','t')",
               (node, "accepted" if accepted_at else "review_requested", node))
    if accepted_at:
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES (?,?,'test','accepted','active','accepted',?)", (f"accept-{node}", node, accepted_at))
    else:
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES (?,?,'test','review_rejected','active','review_requested','2026-09-30T12:00:00Z')", (f"reject-{node}", node))


def _usage(db, usage_id: str, node: str | None, model: str, *, source: str = "external_harness:claude-code",
           at: str = "2026-09-29T12:00:00Z", manifest: str | None = None, completion: int | None = 0, cache_read: int = 0) -> None:
    total = None if completion is None else 1_000_000 + completion + cache_read
    plan = ("plan", node, "attempt", node, "execution") if node else (None,) * 5
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role,context_manifest_id) VALUES (?, 'root',?,1000000,?,0,?,?,?,?,?,?,?,?,?,?)",
               (usage_id, model, completion, cache_read, total, source, at, *plan, manifest))


def _costs(db) -> list[dict]:
    return query_model_costs(db, {**SCOPE, "grain": GRAIN}, IdentityResolver(db))


def _outcome(db, model: str, **usage) -> tuple[str, str, float | None]:
    _task(db, "task")
    _usage(db, "call", "task", model, **usage)
    (row,) = _costs(db)
    return row["model"], row["outcome"], row["dollars"]


def test_cost_runs_through_cost_admission_so_unproven_usage_stays_unknown(db):
    assert _outcome(db, "codex/gpt-5.6-terra", source="external_harness:codex", cache_read=500) == (
        "codex/gpt-5.6-terra", "contract_unproven", None)


def test_unreported_tokens_stay_unknown_never_zero(db):
    assert _outcome(db, "claude-code/claude-opus-5-5", completion=None) == (
        "claude-code/claude-opus-5-5", "tokens_unreported", None)


def test_spark_stays_unpriced_under_its_own_identity(db):
    _task(db, "task")
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES (?, 'root','codex/gpt-5.3-codex-spark',1000,0,0,0,1000,'external_harness:codex','2026-09-29T12:00:00Z','plan','task','attempt','task','execution')", (RECEIPT.format(0),))
    (row,) = _costs(db)
    assert (row["model"], row["outcome"], row["dollars"]) == ("codex/gpt-5.3-codex-spark", "no_list_price", None)


def test_alias_resolved_by_its_manifest_prices_as_that_identity(db):
    seed_manifest(db, "m", "claude-code", "claude-opus-5-5", "catalog")
    model, outcome, dollars = _outcome(db, "claude-code/opus", manifest="m")
    assert (model, outcome) == ("claude-code/claude-opus-5-5", "priced") and dollars and dollars > 0


def test_local_model_is_a_row_with_tasks_and_no_list_price(db):
    for index in range(14):
        _task(db, f"t{index}")
        _usage(db, f"u{index}", f"t{index}", LOCAL_ID, source="conversation")
    built = models_payload(db, SCOPE, GRAIN, [])
    (row,) = built["rows"]
    assert (row["model"], row["label"], row["tasks"]) == (LOCAL_MODEL, "Fortissimo", 14)
    assert (row["recorded_cost_per_task"], row["cost_class"]) == (None, "local")


def test_a_call_that_failed_to_record_before_acceptance_marks_the_served_rate(db):
    for index in range(14):
        _task(db, f"t{index}")
        _usage(db, f"u{index}", f"t{index}", "claude-code/claude-opus-5-5")
    failed = "INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) VALUES (?,?,'test','usage_recording_failed',?)"
    db.execute(failed, ("late", "t1", "2026-10-01T12:00:00Z"))  # after acceptance: outside the figure
    (row,) = models_payload(db, SCOPE, GRAIN, [])["rows"]
    assert row["recording_gap"] is False
    db.execute(failed, ("gap", "t2", "2026-09-29T12:00:00Z"))
    (row,) = models_payload(db, SCOPE, GRAIN, [])["rows"]
    assert row["recording_gap"] is True and row["recorded_cost_per_task"] is not None


def test_one_models_read_walks_the_chains_once_and_leaves_no_table(db, monkeypatch):
    walks = []
    ctes = measurement_model_cost.chain_ctes
    monkeypatch.setattr(measurement_model_cost, "chain_ctes", lambda **kwargs: walks.append(kwargs) or ctes(**kwargs))
    _task(db, "task")
    _usage(db, "call", "task", "claude-code/claude-opus-5-5")
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) "
               "VALUES ('gap','task','test','usage_recording_failed','2026-09-29T12:00:00Z')")
    (row,) = models_payload(db, SCOPE, GRAIN, [])["rows"]
    assert row["tasks"] == 1 and row["recording_gap"] is True
    assert len(walks) == 1
    assert db.execute("SELECT name FROM sqlite_temp_master WHERE name='measurement_task_chain'").fetchone() is None


def test_tasks_are_accepted_tasks_only(db):
    _task(db, "accepted")
    _task(db, "sent-back", accepted_at=None)
    _usage(db, "accepted-call", "accepted", "claude-code/claude-opus-5-5")
    _usage(db, "sent-back-call", "sent-back", "claude-code/claude-opus-5-5")
    assert {row["task_id"] for row in _costs(db)} == {"accepted"}


def test_token_share_follows_the_selected_range(db):
    _usage(db, "early", None, "claude-code/claude-opus-5-5", at="2026-09-08T12:00:00Z")
    built = models_payload(db, SCOPE, GRAIN, [])
    assert [row["model"] for row in built["token_shares"]] == ["claude-code/claude-opus-5-5"]


def test_tasks_and_prices_read_only_the_two_windows_models_shows(db, monkeypatch):
    seen = []
    walk = measurement_models.materialize_chain
    monkeypatch.setattr("api.measurement_models.materialize_chain",
                        lambda _db, params: seen.append(params["grain"]) or walk(_db, params))
    models_payload(db, SCOPE, GRAIN, [])
    (read,) = seen
    # Eight of the twelve periods, the grain's own windows unchanged.
    assert read.periods == GRAIN.previous.periods + GRAIN.latest.periods
    assert (read.latest, read.previous) == (GRAIN.latest, GRAIN.previous)


def test_an_external_alibaba_identity_gets_its_regions_rate_by_the_hour_of_its_call(db):
    seed_external_server(db, "ns", ALIBABA_HOST)
    seed_external_server(db, "bj", "ws-1.cn-beijing.maas.aliyuncs.com")
    for node, server, at in (("busy", "ns", "2026-09-29T12:00:00Z"), ("idle", "ns", "2026-09-29T15:00:00Z"),
                             ("beijing", "bj", "2026-09-29T12:00:00Z")):
        _task(db, node)
        _usage(db, node, node, f"external@{server}/deepseek-v4.1-flash", source="conversation", at=at,
               completion=1_000_000, cache_read=1_000_000)
    rows = {row["task_id"]: row for row in _costs(db)}
    assert {task: (row["outcome"], row["dollars"]) for task, row in rows.items()} == {
        "busy": ("priced", pytest.approx(0.3 + 1.2 + 0.03)),
        "idle": ("priced", pytest.approx(0.15 + 0.6 + 0.015)),
        "beijing": ("priced", pytest.approx(0.283 + 1.131 + 0.0283)),
    }


def _sized(costs, sizes: dict[str, str], first_try: frozenset[str] = frozenset()) -> dict[str, list[dict]]:
    """Rows' size cells, with ``sizes`` naming each task's class and ``first_try`` its first-try tasks."""
    outcomes = {cost["task_id"]: (sizes.get(cost["task_id"]), int(cost["task_id"] in first_try)) for cost in costs}
    built = build_models([], [], [], list(costs), resolve=_resolve, latest=(LATEST,), previous=(PREVIOUS,),
                         offered=set(), outcomes=outcomes)
    return {row["model"]: row["sizes"] for row in built["rows"]}


def test_a_size_cell_is_the_rate_of_that_class_with_its_change_and_first_try_share():
    latest, previous = _period("m", 14, dollars=2.0), _period("m", 14, period=PREVIOUS, dollars=1.0)
    small = _period("m", 3, dollars=9.0)  # three S tasks: under the floor, so no S cell
    for task in small:
        task["task_id"] += "-s"
    unsized = _period("m", 5, dollars=50.0)  # tasks with no size count in the row, never in a cell
    for task in unsized:
        task["task_id"] += "-u"
    costs = latest + previous + small + unsized
    sizes = {cost["task_id"]: "M" for cost in latest + previous} | {cost["task_id"]: "S" for cost in small}
    first = frozenset(cost["task_id"] for cost in latest[:7])
    (cell,) = _sized(costs, sizes, first)["m"]
    assert cell == {"size": "M", "tasks": 14, "cost_per_task": 2.0, "previous_cost_per_task": 1.0, "cost_change": pytest.approx(1.0),
                    "change_reason": "none", "first_try": 0.5, "partial": False, "recording_gap": False}


def test_a_size_cell_change_is_new_when_the_previous_period_had_under_fourteen_of_that_class():
    costs = _period("m", 14, dollars=2.0) + _period("m", 13, period=PREVIOUS, dollars=1.0)
    (cell,) = _sized(costs, {cost["task_id"]: "L" for cost in costs})["m"]
    assert (cell["size"], cell["cost_change"], cell["change_reason"]) == ("L", None, "new")


def test_a_class_with_no_priced_dollars_has_no_cell():
    local = _period("m", 14, outcome="not_api_priced", dollars=None)
    unpriced = _period("u", 14, outcome="no_pricing", dollars=None)
    cells = _sized(local + unpriced, {cost["task_id"]: "M" for cost in local + unpriced})
    assert cells == {"m": [], "u": []}


def test_the_overall_size_cells_pool_every_models_tasks_of_a_class_whatever_each_models_own_cell():
    costs = [*_period("a", 7, dollars=2.0), *_period("b", 7, dollars=4.0),
             *_period("a", 7, PREVIOUS, dollars=1.0), *_period("b", 7, PREVIOUS, dollars=1.0)]
    outcomes = {cost["task_id"]: ("S", 0) for cost in costs}
    built = build_models([], [], [], costs, resolve=_resolve, latest=(LATEST,), previous=(PREVIOUS,),
                         offered=set(), outcomes=outcomes)
    # Neither model has 14 S tasks alone; together they do, and that is the class's figure.
    (cell,) = built["size_overall"]
    assert (cell["size"], cell["tasks"], cell["cost_per_task"], cell["previous_cost_per_task"]) == ("S", 14, 3.0, 1.0)


def test_the_floor_rows_cells_pool_its_members_tasks():
    costs = [*_period("big", 14), *_period("a", 7, dollars=2.0), *_period("b", 7, dollars=4.0)]
    rows = _sized(costs, {cost["task_id"]: "S" for cost in costs if not cost["task_id"].startswith("big")})
    assert rows["big"] == []
    (cell,) = rows[""]
    assert (cell["size"], cell["tasks"], cell["cost_per_task"], cell["change_reason"]) == ("S", 14, 3.0, "new")


def test_cells_read_each_tasks_declared_size_and_first_try_from_the_outcome(db):
    for index in range(14):
        _task(db, f"t{index}")
        _usage(db, f"u{index}", f"t{index}", "claude-code/claude-opus-5-5")
    db.execute("UPDATE plan_nodes SET size='M'")
    # A sent-back task accepted later is not a first try.
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) VALUES ('r','t0','test','review_rejected','2026-09-29T12:00:00Z')")
    (row,) = models_payload(db, SCOPE, GRAIN, [])["rows"]
    (cell,) = row["sizes"]
    assert (cell["size"], cell["tasks"], cell["first_try"]) == ("M", 14, pytest.approx(13 / 14))
    assert cell["cost_per_task"] == pytest.approx(row["recorded_cost_per_task"])
