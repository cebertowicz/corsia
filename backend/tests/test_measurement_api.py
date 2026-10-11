"""Focused tests for the weekly measurement API."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException


USER = {"id": "steve"}
WEEK = "2026-09-21"


# The families the frozen legacy oracle implements. This set is FROZEN ON PURPOSE:
# it is the retirement boundary of the equivalence proof, not an oversight, and it
# must not be widened back to the registry. A newer family gets its own semantic
# fixtures instead of teaching the legacy module a second implementation of itself.
_ORACLE_FAMILIES = frozenset({"outcomes", "active", "presence", "decisions", "tokens"})


def _oracle_token_totals(rows: list[dict]) -> list[dict]:
    """Collapse the oracle's per-harness token lines into the one weekly total now served.

    The oracle stays byte-frozen on the old harness split; the served family sums every
    harness into one line, so the proof compares period, tokens and outcomes only. The
    cache re-read and uncounted-call fields have their own fixtures.
    """
    periods: dict[str, dict] = {}
    for row in rows:
        period = periods.setdefault(row["period"], {"period": row["period"], "tokens": 0, "outcomes": row["outcomes"]})
        period["tokens"] += row["tokens"]
    return list(periods.values())


def _served_token_totals(rows: list[dict]) -> list[dict]:
    return [{key: row[key] for key in ("period", "tokens", "outcomes")} for row in rows]


def _grained(params: dict) -> dict:
    """The oracle's from/to weeks as the page's own request: UTC weeks, last completed week `to`."""
    from datetime import datetime, timedelta, timezone

    from core.measurement_scope import resolve_grain, usage_bounds

    end = date.fromisoformat(params["to"])
    now = datetime.combine(end + timedelta(days=7), datetime.min.time(), timezone.utc)
    from_ts, to_ts = usage_bounds(params["from"], params["to"])
    return {**params, "grain": resolve_grain("weeks", "UTC", now=now), "from_ts": from_ts, "to_ts": to_ts}


def _family_in_range(db, family: str, params: dict) -> list:
    """One family's rows from the page's grain, without the periods before the oracle's `from`
    (the page draws twelve periods; the frozen oracle was only asked for a few)."""
    from api.measurement_queries import query_family

    return [row for row in query_family(db, family, _grained(params)) if str(row["period"]) >= params["from"]]


def _oracle_params(params: dict) -> dict:
    """Translate one composite-scope request into the frozen oracle's exclusive scope.

    The oracle stays byte-frozen on the pre-composite contract, so the mapping
    lives here: a named agent is agent scope, a named project alone is project
    scope, and anything else is the whole workspace.
    """
    agent = params.get("scope_agent")
    project = params.get("scope_project") or "all"
    kind = "agent" if agent else ("project" if project != "all" else "all")
    return {**params, "scope_kind": kind, "scope_id": agent if agent else (project if kind == "project" else None)}


def _seed_thread(db, thread_id: str, *, user_id: str = "steve", project_id: str | None = None,
                 parent_id: str | None = None, agent: bool = False, status: str | None = "active") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,agent_responsibility,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (thread_id, user_id, thread_id, project_id, parent_id, int(agent), status,
         "Owns measurement" if agent else None, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )


def _seed_outcomes(db, thread_id: str, count: int = 762) -> None:
    plan_id = "plan-root" if thread_id == "root" else f"plan-{thread_id}"
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES (?,'thread',?,'active',1,'t','t')", (plan_id, thread_id))
    nodes = [(f"node-{number}" if thread_id == "root" else f"{thread_id}-node-{number}", plan_id, number, f"node-{number}") for number in range(count)]
    db.executemany(
        "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) "
        "VALUES (?, ?, NULL, ?, 'task', 'Measured task', 'done', 'accepted', ?, 1, 1, 't', 't')", nodes,
    )
    db.executemany(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
        "VALUES (?,?,'test','accepted','active','accepted','2026-09-23T12:00:00Z')",
        [(f"event-{number}" if thread_id == "root" else f"{thread_id}-event-{number}", f"node-{number}" if thread_id == "root" else f"{thread_id}-node-{number}") for number in range(count)],
    )


def _seed_decision(db, index: int, answer: str | None, *, raised: str = "2026-09-15T12:00:00Z", thread_id: str = "root", recommendation: str | None = "B") -> None:
    decision: dict[str, object] = {"thread_id": thread_id, "raised_at": raised, "answered_at": "2026-09-23T12:00:00Z", "state": "answered", "recommendation": recommendation}
    if answer is not None:
        decision["answer"] = {"option_id": answer}
    metadata = {"plan_decision": decision}
    db.execute(
        "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
        "VALUES (?, 'local', 'plan-decision', ?, ?, 't', 't')",
        (f"decision-{index}", f"decision-{index}", json.dumps(metadata)),
    )


ALIGNMENT_IDS = ("took_recommendation_pct", "answered_other_pct", "answered_neither_pct")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 5, 6, 9, 10])
@pytest.mark.parametrize("agent_scope", [False, True])
async def test_alignment_common_no_answer_minimum_and_thin_rules(isolated_db, count, agent_scope):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _seed_outcomes(db, "root", count=20)
        for index in range(count):
            _seed_decision(db, index, "A")
        db.commit()
    finally:
        db.close()
    result = await measurement_series(",".join(ALIGNMENT_IDS), project="all", agent="root" if agent_scope else "none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {row["series"]: row for row in result["rows"] if row["period"] == WEEK}
    assert set(rows) == set(ALIGNMENT_IDS)
    for metric, row in rows.items():
        assert row["n"] == count
        assert row["thin"] is (agent_scope and count < 6)
        assert row["partial"] is False
        assert row["comparable"] is True
        assert row["proxy"] is False
        assert row["value"] == (None if count < 10 else int(metric == "answered_neither_pct"))


@pytest.mark.asyncio
@pytest.mark.parametrize("project,agent,expected_n,expected_neither", [
    ("all", "none", 40, 0.5), ("project", "none", 30, 1 / 3),
    ("all", "root", 20, 0.5), ("project", "root", 20, 0.5),
])
async def test_neither_uses_common_all_project_agent_and_descendant_scope(isolated_db, project, agent, expected_n, expected_neither):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Measurement','t','t')")
        _seed_thread(db, "root", project_id="project", agent=True)
        _seed_thread(db, "child", project_id="project", parent_id="root")
        _seed_thread(db, "peer", project_id="project")
        _seed_thread(db, "outside")
        _seed_thread(db, "foreign", user_id="another-user")
        for group, (thread, answer) in enumerate([("root", "B"), ("child", "A"), ("peer", "other"), ("outside", "A"), ("foreign", "A")]):
            _seed_outcomes(db, thread, count=20)
            for index in range(10):
                _seed_decision(db, group * 10 + index, answer, thread_id=thread)
        db.commit()
    finally:
        db.close()
    result = await measurement_series(",".join(ALIGNMENT_IDS), project=project, agent=agent, grain="weeks", timezone_name="UTC", user=USER)
    rows = {row["series"]: row for row in result["rows"] if row["period"] == WEEK}
    assert {row["n"] for row in rows.values()} == {expected_n}
    assert rows["answered_neither_pct"]["value"] == pytest.approx(expected_neither)
    assert sum(row["value"] for row in rows.values()) == pytest.approx(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,recommendation,expected", [
    (None, "B", (0, 0, 0)), ("A", None, (0, 0, 0)),
    ("other", "other", (1, 1, 0)), ("A", "other", (0, 0, 0)),
    ("B", "B", (1, 0, 0)), ("other", "B", (0, 1, 0)), ("A", "B", (0, 0, 1)),
])
async def test_alignment_raw_choices_preserve_missing_and_overlapping_legacy_partitions(isolated_db, answer, recommendation, expected):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=20)
        for index in range(10):
            _seed_decision(db, index, answer, recommendation=recommendation)
        db.commit()
    finally:
        db.close()
    result = await measurement_series(",".join(ALIGNMENT_IDS), project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {row["series"]: row for row in result["rows"] if row["period"] == WEEK}
    assert tuple(rows[metric]["value"] for metric in ALIGNMENT_IDS) == expected
    assert {row["n"] for row in rows.values()} == {10}
    # Missing fields and Other-overlap remain uncovered/overlapping, never normalized.
    if sum(expected) != 1:
        assert sum(row["value"] for row in rows.values()) != 1


@pytest.mark.asyncio
async def test_neither_request_does_not_change_old_alignment_companions(isolated_db):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=20)
        for index in range(10):
            _seed_decision(db, index, ["B", "other", "A"][index % 3])
        db.commit()
    finally:
        db.close()
    old = await measurement_series("took_recommendation_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    explicit = await measurement_series(",".join(ALIGNMENT_IDS), project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert {row["series"] for row in old["rows"]} == set(ALIGNMENT_IDS[:2])
    assert old["rows"] == [row for row in explicit["rows"] if row["series"] != "answered_neither_pct"]
    alone = await measurement_series("answered_neither_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert {row["series"] for row in alone["rows"]} == {"answered_neither_pct"}


@pytest.mark.asyncio
async def test_series_returns_762_aggregate_only_outcomes(isolated_db):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root")
        db.commit()
    finally:
        db.close()

    result = await measurement_series("outcomes", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in result["rows"] if row["n"] == 762)
    assert row["value"] == row["n"] == 762
    assert set(row) == {"period", "series", "value", "raw_value", "n", "proxy", "partial", "comparable", "break", "dimension", "thin", "average"}
    assert not {"thread_id", "node_id", "message_id", "content"} & set(row)


@pytest.mark.asyncio
async def test_decision_uses_raised_period_for_asks_and_answered_period_for_alignment(isolated_db):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root")
        prior_nodes = [(f"prior-{number}", "plan-root", 800 + number, f"prior-{number}") for number in range(20)]
        db.executemany(
            "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) "
            "VALUES (?, ?, NULL, ?, 'task', 'Measured task', 'done', 'accepted', ?, 1, 1, 't', 't')", prior_nodes,
        )
        db.executemany(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES (?,?,'test','accepted','active','accepted','2026-09-16T12:00:00Z')",
            [(f"prior-event-{number}", f"prior-{number}") for number in range(20)],
        )
        for index, answer in enumerate(["B", "B", "B", "B", "A", "other", "other", "other", "other", "other"]):
            _seed_decision(db, index, answer)
        db.commit()
    finally:
        db.close()

    result = await measurement_series("asks_per_outcome,took_recommendation_pct,answered_neither_pct", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {(row["series"], row["period"]): row for row in result["rows"]}
    assert rows[("asks_per_outcome", "2026-09-14")]["value"] == pytest.approx(10 / 20)
    assert rows[("took_recommendation_pct", WEEK)]["value"] == pytest.approx(0.4)
    assert rows[("answered_other_pct", WEEK)]["value"] == pytest.approx(0.5)
    assert rows[("answered_neither_pct", WEEK)]["value"] == pytest.approx(0.1)
    assert rows[("answered_neither_pct", WEEK)]["n"] == 10


@pytest.mark.asyncio
async def test_active_time_counts_300_and_600_but_not_601_second_gaps(isolated_db):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root")
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?, 'root', 'user', 'x', '{}', ?)",
            [("m1", "2026-09-22T10:00:00Z"), ("m2", "2026-09-22T10:05:00Z"),
             ("m3", "2026-09-22T10:15:00Z"), ("m4", "2026-09-22T10:25:01Z")],
        )
        db.commit()
    finally:
        db.close()

    result = await measurement_series("active_hours_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in result["rows"] if row["n"] == 762)
    assert row["value"] == pytest.approx((4 * 60 + 300 + 600) / 3600 / 762)


def test_outcomes_per_active_hour_is_proxy_and_handles_zero_hours():
    from api.measurement import _row

    row = _row("outcomes_per_active_hour", WEEK, {"outcomes": 40, "active_hours": 8}, False)
    assert row["value"] == pytest.approx(5)
    assert row["proxy"] is True
    assert _row("outcomes_per_active_hour", WEEK, {"outcomes": 40, "active_hours": 0}, False)["value"] is None


@pytest.mark.asyncio
async def test_active_time_uses_the_single_pre_window_predecessor(isolated_db):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root")
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?, 'root', 'user', 'x', '{}', ?)",
            [("before-window", "2026-09-20T23:55:00Z"), ("in-window", "2026-09-21T00:00:00Z")],
        )
        db.commit()
    finally:
        db.close()

    result = await measurement_series("active_hours_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert next(row for row in result["rows"] if row["n"] == 762)["value"] == pytest.approx(360 / 3600 / 762)


@pytest.mark.asyncio
async def test_agent_null_status_is_active_and_scope_limits_descendants(isolated_db):
    from api.agents import list_agents
    from api.measurement import measurement_index
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Measurement','t','t')")
        _seed_thread(db, "agent", agent=True, status=None)
        _seed_thread(db, "child", parent_id="agent")
        _seed_thread(db, "project-root", project_id="project")
        _seed_thread(db, "inactive", agent=True, status="inactive")
        db.commit()
    finally:
        db.close()

    roster = await list_agents(user=USER)
    assert [agent["thread_id"] for agent in roster["agents"]] == ["agent"]
    assert (await measurement_index(project="all", agent="none", user=USER))["scope"] == {"project": "all", "agent": None}
    assert (await measurement_index(project="project", agent="none", user=USER))["scope"] == {"project": "project", "agent": None}
    assert (await measurement_index(project="all", agent="agent", user=USER))["scope"] == {"project": "all", "agent": "agent"}
    with pytest.raises(HTTPException) as error:
        await measurement_index(project="all", agent="inactive", user=USER)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_a_selected_agent_inside_a_family_still_measures_its_own_work(isolated_db):
    """A roster agent promoted from a family thread is still measurable.

    Selecting it used to validate and then serve nothing, because the closure
    roots demanded a top-level parent.
    """
    from api.measurement import measurement_index, measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "sponsor")
        _seed_thread(db, "agent", agent=True, parent_id="sponsor")
        _seed_thread(db, "descendant", parent_id="agent")
        _seed_outcomes(db, "agent")
        db.commit()
    finally:
        db.close()

    assert (await measurement_index(project="all", agent="agent", user=USER))["scope"] == {"project": "all", "agent": "agent"}
    result = await measurement_series("outcomes", project="all", agent="agent", grain="weeks", timezone_name="UTC", user=USER)
    measured = [row for row in result["rows"] if row["series"] == "outcomes" and row["n"]]
    assert measured, "selecting an agent that has a parent served no outcome row at all"
    assert (measured[0]["value"], measured[0]["n"], measured[0]["period"]) == (762, 762, "2026-09-21")


@pytest.mark.asyncio
async def test_tokens_are_one_total_line_and_index_marks_dollars_unavailable(isolated_db):
    from api.measurement import measurement_index, measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root")
        db.execute("INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) VALUES ('manifest','native','[]',0,'t')")
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at,context_manifest_id) VALUES (?,?,?,?,?,?,?,?)",
            [("usage-native", "root", "test", 7620, 1.0, "test", "2026-09-23T12:00:00Z", "manifest"),
             ("usage-unknown", "root", "test", 762, None, "test", "2026-09-23T12:00:00Z", None)],
        )
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    # One line across every harness: (7620 + 762) / 762, with no per-harness split and no estimate flag.
    assert {(row["dimension"], row["value"], row["proxy"]) for row in series["rows"] if row["value"] is not None} == {
        (None, 11.0, False),
    }
    index = await measurement_index(project="all", agent="none", user=USER)
    definitions = {item["id"]: item for item in index["definitions"]}
    assert definitions["dollar_trend"]["availability"] == "unavailable"
    assert definitions["asks_per_outcome"]["breaks"] == [{"period": "2026-09-07", "kind": "definition"}]
    assert index["cost_metadata"] == {"known_cost_usd": 1.0, "unknown_cost_share": 0.5}


def _named_rows(served):
    """Per-model rows; models under the task floor share the Other row instead."""
    named = [row for row in served["rows"] if not row.get("other")]
    for row in served["rows"]:
        if row.get("other"):
            assert row["tasks"] == 0 and row["recorded_cost_per_task"] is None
            assert row["rate_reason"] == "no_tasks"
    return named



def _freeze_clock(monkeypatch, instant=None):
    """Pin the clock ``resolve_grain`` reads: Wednesday 2026-10-07, so the weeks grain's latest window is Sep 7 to Oct 5."""
    from datetime import datetime, timezone
    from core import measurement_scope

    fixed = instant or datetime(2026, 10, 7, 12, tzinfo=timezone.utc)

    class _Clock(measurement_scope.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None or fixed.tzinfo is not None else fixed.replace(tzinfo=tz)

    monkeypatch.setattr(measurement_scope, "datetime", _Clock)

@pytest.mark.asyncio
async def test_models_omits_token_only_models_without_task_activity(isolated_db, monkeypatch):
    """The resolved table does not invent task activity from thread usage alone."""
    _freeze_clock(monkeypatch)
    from api.measurement import measurement_models
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=30)
        db.execute("INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) VALUES ('manifest','native','[]',0,'t')")
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at,context_manifest_id) VALUES (?,?,?,?,?,?,?,?)",
            [("usage-opus", "root", "claude-code/opus", 600, 3.0, "conversation", "2026-09-23T12:00:00Z", "manifest"),
             ("usage-opus-checkpoint", "root", "claude-code/opus", 100, 0.5, "checkpoint", "2026-09-23T12:00:00Z", "manifest"),
             ("usage-codex", "root", "codex/terra", 200, None, "conversation", "2026-09-23T12:00:00Z", None)],
        )
        db.commit()
    finally:
        db.close()

    served = await measurement_models(project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert _named_rows(served) == []
    assert served["definition_version"] == 3


@pytest.mark.asyncio
async def test_models_does_not_show_turn_only_raw_selectors(isolated_db, monkeypatch):
    from api.measurement import measurement_models
    from infra.database import get_db

    _freeze_clock(monkeypatch)
    db = get_db()
    try:
        _seed_thread(db, "root")
        db.execute("INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) VALUES ('manifest','native','[]',0,'t')")
        db.execute("INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at,context_manifest_id) VALUES ('usage','root','claude-code/opus',100,1,'test','2026-09-23T12:00:00Z','manifest')")
        turns = []
        for number in range(50):
            error = "socket closed" if number == 0 else ("usage limit reached" if number == 1 else None)
            turns.append((f"opus-{number}", "root", number, "claude-code/opus", "error" if error else "completed", "2026-09-29T12:00:00Z", "2026-09-29T12:01:00Z", error, "boot", "manifest"))
        # Failed selectors without a Models row are reported separately, not invented as rows.
        turns.extend((f"unmatched-{number}", "root", 100 + number, "claude-code/opus[1m]", "error", "2026-09-29T12:00:00Z", "2026-09-29T12:01:00Z", "socket closed", "boot", "manifest") for number in range(50))
        db.executemany("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,error,boot_epoch,context_manifest_id) VALUES (?,?,?,?,?,?,?,?,?,?)", turns)
        db.commit()
    finally:
        db.close()

    served = await measurement_models(project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert _named_rows(served) == []
    # A model with no spend and under 14 tasks has no row, so turns alone list nothing; the unresolved
    # alias failures are disclosed, and provider limits are not failures.
    assert served["rows"] == []
    assert served["unmatched_failed_turns"] == 51


@pytest.mark.asyncio
async def test_models_does_not_repeat_scope_outcomes_without_node_usage(isolated_db, monkeypatch):
    """Token rows never acquire task counts through thread-tree ancestry alone."""
    _freeze_clock(monkeypatch)
    from api.measurement import measurement_models
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root-a", agent=True)
        _seed_thread(db, "child-a", parent_id="root-a", agent=True)
        _seed_outcomes(db, "root-a", count=20)
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES (?,?,?,?,?,?,?)",
            [("usage-root", "root-a", "model/only-on-root", 100, 1.0, "test", "2026-09-23T12:00:00Z"),
             ("usage-child", "child-a", "model/only-on-child", 900, 9.0, "test", "2026-09-23T12:00:00Z")],
        )
        db.commit()
    finally:
        db.close()

    served = await measurement_models(project="all", agent="root-a", grain="weeks", timezone_name="UTC", user=USER)
    assert _named_rows(served) == []


@pytest.mark.asyncio
async def test_models_range_excludes_usage_that_predates_the_window(isolated_db, monkeypatch):
    """A pair that only ran outside the chosen range is absent, not zero-share.

    Two agents, one range: without the window on the usage read, model B's tokens
    from an earlier range would join the scope total and quietly mix two ranges into
    every share.
    """
    from api.measurement import measurement_models
    from infra.database import get_db

    _freeze_clock(monkeypatch)
    db = get_db()
    try:
        _seed_thread(db, "root-a")
        _seed_thread(db, "root-b", agent=True)
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES (?,?,?,?,?,?,?)",
            [("usage-a-now", "root-a", "model/now", 100, 1.0, "test", "2026-09-23T12:00:00Z"),
             ("usage-b-then", "root-b", "model/then", 4000, 40.0, "test", "2026-05-12T12:00:00Z")],
        )
        db.commit()
    finally:
        db.close()

    served = await measurement_models(project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert _named_rows(served) == []
    assert [row["model"] for row in served["token_shares"]] == ["model/now"]
@pytest.mark.asyncio
async def test_models_endpoint_compares_real_previous_window_tasks_and_cost(isolated_db, monkeypatch):
    from api.measurement import measurement_models
    from infra.database import get_db

    _freeze_clock(monkeypatch)
    db = get_db()
    try:
        _seed_thread(db, "root")
        db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','root','active',1,'t','t')")
        for period, accepted_at in [("previous", "2026-08-19T12:00:00Z"), ("latest", "2026-09-23T12:00:00Z")]:
            for index in range(14):
                node = f"{period}-{index}"
                db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES (?, 'plan',NULL,1,'task','task','done','accepted',?,1,1,'t','t')", (node, node))
                db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES (?,?,'test','accepted','active','accepted',?)", (f"accepted-{node}", node, accepted_at))
                db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES (?, 'root','claude-code/claude-opus-5-5',1000000,0,0,0,1000000,'external_harness:claude-code',?,'plan',?,'attempt',?,'execution')", (f"usage-{node}", accepted_at, node, node))
        db.commit()
    finally:
        db.close()

    served = await measurement_models(project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in served["rows"] if row["model"] == "claude-code/claude-opus-5-5")
    assert row["tasks"] == row["previous_tasks"] == 14
    assert row["recorded_cost_per_task"] == pytest.approx(4.0)
    assert row["cost_change"] == 0.0
    assert (row["rate_reason"], row["change_reason"]) == ("none", "none")


def test_aggregate_families_exclude_out_of_window_rows(isolated_db):
    from api.measurement_queries import prepare_outcome_periods as _prepare_outcome_periods, query_family as _query_family
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=1)
        db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('outside-message','root','user','x','{}','2026-09-23T12:00:00Z')")
        db.execute("INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) VALUES ('steve','thread','root','2026-09-23T12:00:00Z','2026-09-23T12:01:00Z',60,1,'hidden')")
        _seed_decision(db, 1, "B", raised="2026-09-22T12:00:00Z")
        db.execute("INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES ('outside-usage','root','test',10,1.0,'test','2026-09-23T12:00:00Z')")
        db.commit()
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
                  "from": "2026-09-14", "to": "2026-09-14", "max_active_gap_seconds": 600}
        _prepare_outcome_periods(db, _grained(params))
        assert all(not _query_family(db, family, _grained(params)) for family in ("outcomes", "active", "presence", "decisions", "tokens"))
    finally:
        db.close()


def test_prepared_outcomes_and_sargable_ranges_match_legacy_family_values(isolated_db):
    """Every published series gets the exact aggregate values from the prior SQL."""
    from api.measurement import REGISTRY
    from api.measurement_queries import prepare_outcome_periods as _prepare_outcome_periods, query_family as _query_family
    from infra.database import get_db
    from tests.measurement_legacy_queries import query_legacy_family

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=20)
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?, 'root', 'user', 'x', '{}', ?)",
            [("before", "2026-09-13T23:55:00Z"), ("during", "2026-09-21T00:05:00Z")],
        )
        db.execute("INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) VALUES ('steve','thread','root','2026-09-21T12:00:00Z','2026-09-21T12:01:00Z',60,1,'hidden')")
        _seed_decision(db, 1, "B", raised="2026-09-21T12:00:00Z")
        db.execute("INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES ('usage','root','test',20,1.0,'test','2026-09-21T12:00:00Z')")
        db.commit()
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
                  "from": "2026-09-14", "to": "2026-09-28", "max_active_gap_seconds": 600}
        # The oracle is the frozen legacy SQL, and only for the families it was written
        # against; the ``models`` family reads rows the legacy module never covered,
        # so its fixtures live in this file instead.
        families = _ORACLE_FAMILIES & {definition.family for definition in REGISTRY.values() if definition.available}
        legacy = {family: query_legacy_family(db, family, _oracle_params(params)) for family in families}

        _prepare_outcome_periods(db, _grained(params))
        optimized = {family: _family_in_range(db, family, params) for family in families}
        # The frozen legacy oracle predates the explicit Neither numerator. Its comparison
        # remains a regression check for the old fields, while Neither has focused fixtures.
        optimized["decisions"] = [{key: value for key, value in row.items() if key != "neither"} for row in optimized["decisions"]]
        legacy["tokens"] = _oracle_token_totals(legacy["tokens"])
        optimized["tokens"] = _served_token_totals(optimized["tokens"])

        assert optimized == legacy
    finally:
        db.close()


def test_pre_window_predecessor_across_a_thread_tree_matches_legacy(isolated_db):
    """The latest human message before the window is picked across every scope
    thread, grandchildren included, exactly as the prior single sorted query did."""
    from api.measurement_queries import prepare_outcome_periods as _prepare_outcome_periods, query_family as _query_family
    from infra.database import get_db
    from tests.measurement_legacy_queries import query_legacy_family

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_thread(db, "child", parent_id="root")
        _seed_thread(db, "grandchild", parent_id="child")
        _seed_thread(db, "other-root")
        _seed_thread(db, "foreign", user_id="someone-else")
        _seed_outcomes(db, "root", count=1)
        rows = [
            ("a-root", "root", "user", "{}", "2026-09-13T23:50:00Z"),
            # Each thread's latest counts, not its earliest.
            ("a-early", "grandchild", "user", "{}", "2026-09-13T23:00:00Z"),
            ("a-early-other", "other-root", "user", "{}", "2026-09-13T23:00:00Z"),
            # Tied latest pre-window human messages in two threads; the higher id wins.
            ("b-tie", "other-root", "user", "{}", "2026-09-13T23:58:00Z"),
            ("c-tie", "grandchild", "user", "{}", "2026-09-13T23:58:00Z"),
            # Later, but not a human message: never the predecessor.
            ("d-agent", "child", "user", '{"sender_kind": "agent"}', "2026-09-13T23:59:00Z"),
            ("e-reply", "root", "assistant", "{}", "2026-09-13T23:59:30Z"),
            ("f-foreign", "foreign", "user", "{}", "2026-09-13T23:59:45Z"),
            ("g-during", "child", "user", "{}", "2026-09-14T00:05:00Z"),
            ("h-during", "grandchild", "user", "{}", "2026-09-14T00:09:00Z"),
        ]
        db.executemany("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?,?,'x',?,?)", rows)
        db.commit()
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
                  "from": "2026-09-14", "to": "2026-09-14", "max_active_gap_seconds": 600}
        legacy = query_legacy_family(db, "active", _oracle_params(params))

        _prepare_outcome_periods(db, _grained(params))
        optimized = _family_in_range(db, "active", params)

        # 2 human messages in the window, gaps 7 minutes (from the tied 23:58
        # predecessor) and 4 minutes, both under 600s.
        assert legacy == [{"period": "2026-09-14", "active_hours": pytest.approx((2 * 60 + 420 + 240) / 3600), "outcomes": 0}]
        assert optimized == legacy
    finally:
        db.close()


def test_range_bounds_match_legacy_for_space_separated_timestamps(isolated_db):
    """Rows stored as 'YYYY-MM-DD HH:MM:SS' bucket like the date()-based prior SQL at both edges."""
    from api.measurement_queries import prepare_outcome_periods as _prepare_outcome_periods, query_family as _query_family
    from infra.database import get_db
    from tests.measurement_legacy_queries import query_legacy_family

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=1)
        for index, (suffix, stamp) in enumerate((("first", "2026-09-14 00:30:00"), ("after", "2026-10-05 00:30:00"))):
            db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,'root','user','x','{}',?)", (f"m-{suffix}", stamp))
            db.execute("INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) VALUES ('steve','thread','root',?,?,60,1,'hidden')", (stamp, stamp))
            db.execute("INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) VALUES (?,'root','test',10,1.0,'test',?)", (f"u-{suffix}", stamp))
            _seed_decision(db, index, "B", raised=stamp)
        db.commit()
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
                  "from": "2026-09-14", "to": "2026-09-28", "max_active_gap_seconds": 600}
        families = ("active", "presence", "decisions", "tokens")
        legacy = {family: query_legacy_family(db, family, _oracle_params(params)) for family in families}

        _prepare_outcome_periods(db, _grained(params))
        optimized = {family: _family_in_range(db, family, params) for family in families}
        optimized["decisions"] = [{key: value for key, value in row.items() if key != "neither"} for row in optimized["decisions"]]
        legacy["tokens"] = _oracle_token_totals(legacy["tokens"])
        optimized["tokens"] = _served_token_totals(optimized["tokens"])

        assert all(legacy[family] for family in families)
        assert optimized == legacy
    finally:
        db.close()


@pytest.mark.asyncio
async def test_validation_rejects_unknown_ids_and_grains(isolated_db):
    from api.measurement import measurement_series

    with pytest.raises(HTTPException) as unknown:
        await measurement_series("unknown", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    with pytest.raises(HTTPException) as invalid_grain:
        await measurement_series("outcomes", project="all", agent="none", grain="months", timezone_name="UTC", user=USER)
    assert unknown.value.status_code == invalid_grain.value.status_code == 400


@pytest.mark.asyncio
async def test_asks_series_starts_where_user_owned_holds_were_recorded_so_it_has_no_definition_break(isolated_db):
    """No served asks row predates the definition change, so no row carries a break the chart must mark."""
    from api.measurement import measurement_series

    result = await measurement_series("asks_per_outcome,outcomes", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {(row["series"], row["period"]): row for row in result["rows"]}

    assert ("asks_per_outcome", "2026-08-24") not in rows and ("asks_per_outcome", "2026-08-31") not in rows
    assert rows[("asks_per_outcome", "2026-09-07")]["break"] is None
    assert all(row["break"] is None and row["comparable"] for row in result["rows"] if row["series"] == "outcomes")


@pytest.mark.asyncio
async def test_each_series_is_the_same_alone_or_batched(isolated_db, monkeypatch):
    """The page asks for every panel's ids in one request; an id's rows must not depend on its neighbours.

    A shared spine would start every id at the earliest period among the requested ids,
    so a batched page would give a later-starting series leading periods it never had alone.
    """
    from api.measurement import measurement_series
    from api.measurement_registry import REGISTRY

    _pin_series_now(monkeypatch, 2026, 10, 8, 12, 0)
    ids = [metric for metric, definition in REGISTRY.items() if definition.available]
    for grain in ("weeks", "days"):
        batch = await measurement_series(",".join(ids), project="all", agent="none", grain=grain, timezone_name="UTC", user=USER)
        assert batch["rows"], grain
        for metric in ids:
            alone = await measurement_series(metric, project="all", agent="none", grain=grain, timezone_name="UTC", user=USER)
            expected = [row for row in alone["rows"] if row["series"] == metric]
            assert [row for row in batch["rows"] if row["series"] == metric] == expected, (grain, metric)
            assert batch["windows"][metric] == alone["windows"][metric], (grain, metric)


@pytest.mark.asyncio
async def test_quality_all_range_serves_the_measured_period(isolated_db):
    """The Quality pair's data starts mid-week, and All must still serve its rows.

    Both series start on a day that is not a Monday. The All spine was stepped
    from that day, matched no Monday-keyed row, and served every week as
    unmeasured while outcomes existed.
    """
    from datetime import date

    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root")
        db.commit()
    finally:
        db.close()

    result = await measurement_series(
        "first_try_acceptance,rejections_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER
    )
    assert result["rows"] and all(date.fromisoformat(row["period"]).weekday() == 0 for row in result["rows"])
    rows = {(row["series"], row["period"]): row for row in result["rows"]}
    assert (rows[("first_try_acceptance", WEEK)]["n"], rows[("first_try_acceptance", WEEK)]["value"]) == (762, 1.0)
    assert (rows[("rejections_per_outcome", WEEK)]["n"], rows[("rejections_per_outcome", WEEK)]["value"]) == (762, 0.0)


def test_http_sparse_token_periods_serve_one_total_row_per_period(isolated_db):
    """Every period is one row across harnesses, and a period with no usage is a gap, not a zero."""
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_plan(db, "sparse-plan", "root")
        for period in ("2026-09-14", "2026-09-21"):
            for number in range(24):
                _seed_task(db, "sparse-plan", f"{period}-{number}", settled=f"{period}T12:00:00Z")
        _seed_manifest(db)
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES (?, 'root', 'test', ?, 0.0, 'test', ?, ?)",
            [("sparse-native", 480, "2026-09-21T12:00:00Z", "manifest"),
             ("sparse-unknown", 240, "2026-09-14T12:00:00Z", None)],
        )
        db.commit()
    finally:
        db.close()

    response = _measurement_client().get(
        "/api/measurement/series?ids=fresh_tokens_per_outcome&grain=weeks&timezone=UTC&project=all&agent=none"
    )
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    periods = {row["period"] for row in rows}
    assert len(periods) == len(rows), "one row per period: the two harnesses are one line"
    assert {row["dimension"] for row in rows} == {None}
    for row in rows:
        expected = {"2026-09-21": 20, "2026-09-14": 10}.get(row["period"])
        assert (row["value"], row["n"]) == (expected, 24 if expected is not None else 0)
        assert row["proxy"] is False
        assert row["partial"] is False


def test_http_empty_token_family_serves_one_gap_per_period(isolated_db):
    """An empty token family still serves one gap per period, on the one total line."""
    response = _measurement_client().get(
        "/api/measurement/series?ids=fresh_tokens_per_outcome&grain=weeks&timezone=UTC&project=all&agent=none"
    )
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert rows
    assert len({row["period"] for row in rows}) == len(rows)
    assert all(row["dimension"] is None and row["proxy"] is False for row in rows)
    assert all(row["value"] is None and row["n"] == 0 for row in rows)


def _measurement_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import measurement

    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: USER
    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/api/measurement/series?ids=outcomes&grain=weeks&timezone=UTC",
    "/api/measurement/series/index?",
    "/api/measurement/models?grain=weeks&timezone=UTC",
])
@pytest.mark.parametrize("scope", [
    "",
    "&project=all",
    "&agent=none",
    "&scope=project&scope_id=project&from=2026-08-10&to=2026-09-21",
])
def test_http_refuses_a_request_without_the_whole_composite_scope(isolated_db, path, scope):
    """Neither half of the scope has a default at the HTTP edge.

    A request that names no scope, only one half, or the retired exclusive
    parameters was answered with all-work data, as if it had asked for it.
    """
    response = _measurement_client().get(path + scope)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("path", [
    "/api/measurement/series?ids=outcomes&grain=weeks&timezone=UTC",
    "/api/measurement/series/index?",
    "/api/measurement/models?grain=weeks&timezone=UTC",
])
def test_http_serves_the_explicit_composite_scope(isolated_db, path):
    response = _measurement_client().get(path + "&project=all&agent=none")
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == {"project": "all", "agent": None}


# ---------------------------------------------------------------------------
# Accuracy: one outcome definition, one fresh-token measure, real first-try,
# and instruments that serve nothing before they existed.
# ---------------------------------------------------------------------------

_WEEK = "2026-09-23T12:00:00Z"  # settles in the completed period 2026-09-21


def _seed_plan(db, plan_id: str, thread_id: str) -> None:
    db.execute(
        "INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
        "VALUES (?,'thread',?,'active',1,'t','t')",
        (plan_id, thread_id),
    )


def _seed_task(db, plan_id: str, node_id: str, *, settled: str = _WEEK) -> None:
    db.execute(
        "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
        "commitment_id,requirements_revision,revision,created_at,updated_at) "
        "VALUES (?, ?, NULL, 1, 'task', 'Measured task', 'done', 'accepted', ?, 1, 1, 't', 't')",
        (node_id, plan_id, node_id),
    )
    db.execute(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
        "VALUES (?,?, 'test', 'accepted', 'active', 'accepted', ?)",
        (f"event-{node_id}", node_id, settled),
    )


def _seed_usage(db, row_id: str, *, total, prompt, completion, cache_creation, cache_read) -> None:
    db.execute(
        "INSERT INTO llm_usage (id,thread_id,model,total_tokens,prompt_tokens,completion_tokens,"
        "cache_creation_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
        "VALUES (?, 'root', 'test', ?, ?, ?, ?, ?, 0.0, 'test', ?, 'manifest')",
        (row_id, total, prompt, completion, cache_creation, cache_read, _WEEK),
    )


def _seed_manifest(db) -> None:
    db.execute("INSERT INTO context_manifests (id,harness,entries_json,entry_count,created_at) "
               "VALUES ('manifest','native','[]',0,'t')")


@pytest.mark.asyncio
async def test_an_outcome_is_the_work_an_agent_was_asked_to_do_not_its_worker_threads(isolated_db):
    """One definition, everywhere: a spawned worker's tasks are not a second outcome.

    Fail-first on the shipped definition, which counted every one of these 12 accepted
    tasks: an agent that delegated four nodes to workers it spawned reported four times
    the outcomes for the same delivered work. A thread is a spawned worker when
    ``spawned_by`` is set on it or when it is some node's ``spawned_thread_id``; the
    owner's own plan is the one that was asked to do the work. Both workers are seeded as
    roots, because that is what production does: ``threads.spawned_by`` is set on 2,328
    of 3,648 non-persistent roots, which are roots only because ``parent_thread_id`` is
    null, so the shipped top-plan rule lets each of them own outcomes of its own.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "owner")
        _seed_plan(db, "plan-owner", "owner")
        for number in range(3):
            _seed_task(db, "plan-owner", f"own-{number}")
        _seed_task(db, "plan-owner", "spawner")  # the node that spawned, not yet accepted
        db.execute("UPDATE plan_nodes SET state='active' WHERE id='spawner'")
        db.execute("UPDATE plan_nodes SET spawned_thread_id='by-column' WHERE id='spawner'")
        _seed_thread(db, "by-column")
        db.execute("UPDATE threads SET spawned_by='owner' WHERE id='by-column'")
        _seed_plan(db, "plan-by-column", "by-column")
        for number in range(4):
            _seed_task(db, "plan-by-column", f"col-{number}")
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,"
                   "state,commitment_id,requirements_revision,revision,created_at,updated_at,spawned_thread_id) "
                   "VALUES ('spawner2','plan-owner',NULL,2,'task','Delegated','done','active','spawner2',1,1,'t','t','by-node')")
        _seed_thread(db, "by-node")
        _seed_plan(db, "plan-by-node", "by-node")
        for number in range(5):
            _seed_task(db, "plan-by-node", f"nod-{number}")
        db.commit()
    finally:
        db.close()

    result = await measurement_series("outcomes", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in result["rows"] if row["period"] == "2026-09-21")
    assert row["n"] == 3, "the owner's three tasks; the 9 in its two worker threads are not outcomes"


@pytest.mark.asyncio
async def test_a_null_token_total_counts_its_parts_and_cache_re_reads_are_excluded(isolated_db):
    """The one token measure is fresh tokens: what the scope made, cache re-reads out.

    Fail-first on ``SUM(total_tokens)``, which served 100.0 here: it paid 300 of cache
    re-reads on the row that had a total, and silently dropped the row whose total is
    NULL (18,726 of 51,125 usage rows in the 09-14 week alone carried one), so a week's
    figure was the sum of a minority of its rows.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=24)
        _seed_manifest(db)
        _seed_usage(db, "u-total", total=1000, prompt=600, completion=100, cache_creation=0, cache_read=300)
        _seed_usage(db, "u-null", total=None, prompt=500, completion=100, cache_creation=50, cache_read=900)
        _seed_usage(db, "u-zero", total=0, prompt=0, completion=0, cache_creation=0, cache_read=0)
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    value = next(row["value"] for row in series["rows"] if row["period"] == "2026-09-21")
    assert value == 56.25, value  # (700 + 650 + 0) / 24, never (1000 + 0) / 24


@pytest.mark.asyncio
async def test_a_null_total_row_that_carries_cache_reads_is_never_negative(isolated_db):
    """The rebuild adds cache re-reads and subtracts them again, so fresh cannot go below zero.

    The owner's rule, measured: production has zero rows whose ``total - cache_read`` is
    negative, and 17,476 NULL-total rows whose cache re-reads exceed their other parts --
    which is exactly where rebuilding a total from three parts instead of four turned fresh
    tokens negative. Nothing here clamps: the row keeps its own shape.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=24)
        _seed_manifest(db)
        _seed_usage(db, "u-heavy", total=None, prompt=10, completion=0, cache_creation=0, cache_read=1_500_000)
        _seed_usage(db, "u-small", total=5, prompt=3, completion=1, cache_creation=0, cache_read=3)
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    value = next(row["value"] for row in series["rows"] if row["period"] == "2026-09-21")
    assert value == pytest.approx(0.5), value  # (10 + 2) / 24: the 1.5M re-read adds nothing, subtracts nothing
    assert value >= 0


def _pin_series_now(monkeypatch, *moment):
    from api import measurement

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(*moment, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(measurement, "datetime", FixedDateTime)
    monkeypatch.setattr("core.measurement_scope._now", lambda: FixedDateTime.now(timezone.utc))


@pytest.mark.asyncio
async def test_fresh_tokens_total_serves_the_period_its_cache_re_reads_and_its_uncounted_calls(isolated_db):
    """The total is the sum of the same fresh measure, with cache re-reads and uncounted calls beside it.

    Fresh is 700 + 650 + 0; the NULL-total row's 900 and the other row's 300 are cache
    re-reads; the row with no counts at all adds nothing to either figure and is counted
    as one uncounted call, so the period reads as a lower bound instead of as 0 for that call.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_manifest(db)
        _seed_usage(db, "u-total", total=1000, prompt=600, completion=100, cache_creation=0, cache_read=300)
        _seed_usage(db, "u-null", total=None, prompt=500, completion=100, cache_creation=50, cache_read=900)
        _seed_usage(db, "u-zero", total=0, prompt=0, completion=0, cache_creation=0, cache_read=0)
        _seed_usage(db, "u-blank", total=None, prompt=None, completion=None, cache_creation=None, cache_read=None)
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in series["rows"] if row["period"] == "2026-09-21")
    assert (row["series"], row["value"], row["cached"], row["unknown"], row["n"]) == ("fresh_tokens", 1350, 1200, 1, 4)
    assert (row["proxy"], row["partial"], row["comparable"], row["dimension"]) == (False, False, True, None)


@pytest.mark.asyncio
async def test_a_period_of_only_uncounted_calls_has_no_token_figure_never_zero(isolated_db):
    """Unknown stays unknown: a period whose every call carried no count is not 0 tokens."""
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_manifest(db)
        _seed_usage(db, "u-blank", total=None, prompt=None, completion=None, cache_creation=None, cache_read=None)
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in series["rows"] if row["period"] == "2026-09-21")
    assert (row["value"], row["unknown"], row["n"]) == (None, 1, 1)
    assert all(other["value"] is None for other in series["rows"] if other["period"] != "2026-09-21")


@pytest.mark.asyncio
async def test_fresh_tokens_also_serves_the_period_in_progress_flagged_partial(isolated_db, monkeypatch):
    """The token total shows this period so far; every other series stays on completed periods."""
    from api.measurement import measurement_series
    from infra.database import get_db

    _pin_series_now(monkeypatch, 2026, 10, 8, 12, 0)  # a Thursday: the period of 2026-10-05 is in progress
    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=24)
        _seed_manifest(db)
        _seed_usage(db, "u-done", total=1000, prompt=600, completion=100, cache_creation=0, cache_read=300)
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES ('u-now','root','test',500,100,0.0,'test','2026-10-06T09:00:00Z','manifest')"
        )
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens,fresh_tokens_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    totals = [row for row in series["rows"] if row["series"] == "fresh_tokens"]
    assert [(row["period"], row["partial"]) for row in totals][-2:] == [("2026-09-28", False), ("2026-10-05", True)]
    assert {row["period"]: row["value"] for row in totals}["2026-10-05"] == 400
    assert {row["period"]: row["value"] for row in totals}["2026-09-21"] == 700
    per_outcome = [row for row in series["rows"] if row["series"] == "fresh_tokens_per_outcome"]
    assert max(row["period"] for row in per_outcome) == "2026-09-28" and not any(row["partial"] for row in per_outcome)


@pytest.mark.asyncio
async def test_days_serve_today_so_far_flagged_partial_for_the_token_total_within_its_first_hour(isolated_db, monkeypatch):
    """Activity shows within the first hour of the day as a limited row; every other series stays on completed days."""
    from api.measurement import measurement_series
    from infra.database import get_db

    _pin_series_now(monkeypatch, 2026, 10, 8, 0, 40)  # 40 minutes into the UTC day
    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=24)
        _seed_manifest(db)
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES ('u-first-minutes','root','test',500,100,0.0,'test','2026-10-08T00:10:00Z','manifest')"
        )
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens,fresh_tokens_per_outcome", project="all", agent="none", grain="days", timezone_name="UTC", user=USER)
    totals = [row for row in series["rows"] if row["series"] == "fresh_tokens"]
    assert (totals[-1]["period"], totals[-1]["partial"], totals[-1]["value"]) == ("2026-10-08", True, 400)
    per_outcome = [row for row in series["rows"] if row["series"] == "fresh_tokens_per_outcome"]
    assert max(row["period"] for row in per_outcome) == "2026-10-07" and not any(row["partial"] for row in per_outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("today_outcomes", [0, 2])
async def test_days_serve_the_outcome_count_so_far_today_flagged_partial_and_never_a_per_outcome_rate(isolated_db, monkeypatch, today_outcomes):
    """Outcomes also show today so far, a known count even at zero; a rate per outcome has no partial day."""
    from api.measurement import measurement_series
    from infra.database import get_db

    _pin_series_now(monkeypatch, 2026, 10, 8, 0, 40)  # 40 minutes into the UTC day
    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_plan(db, "plan-two", "root")
        _seed_task(db, "plan-two", "yesterday", settled="2026-10-07T12:00:00Z")
        for number in range(today_outcomes):
            _seed_task(db, "plan-two", f"today-{number}", settled="2026-10-08T00:10:00Z")
        _seed_task(db, "plan-two", "tomorrow", settled="2026-10-09T00:00:00Z")
        _seed_manifest(db)
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES ('u-first-minutes','root','test',500,100,0.0,'test','2026-10-08T00:10:00Z','manifest')"
        )
        db.commit()
    finally:
        db.close()

    ids = "outcomes,fresh_tokens,fresh_tokens_per_outcome"
    days = await measurement_series(ids, project="all", agent="none", grain="days", timezone_name="UTC", user=USER)
    outcomes = [row for row in days["rows"] if row["series"] == "outcomes"]
    assert [(row["period"], row["partial"], row["value"]) for row in outcomes][-2:] == [("2026-10-07", False, 1), ("2026-10-08", True, today_outcomes)]
    assert not any(row["partial"] for row in outcomes[:-1])
    per_outcome = [row for row in days["rows"] if row["series"] == "fresh_tokens_per_outcome"]
    assert max(row["period"] for row in per_outcome) == "2026-10-07" and not any(row["partial"] for row in per_outcome)
    totals = [row for row in days["rows"] if row["series"] == "fresh_tokens"]
    assert (totals[-1]["period"], totals[-1]["partial"], totals[-1]["value"]) == ("2026-10-08", True, 400), "a total, whatever today's outcomes"
    periods = await measurement_series("outcomes", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert not any(row["partial"] for row in periods["rows"]), "the Periods grain keeps completed periods only"


@pytest.mark.asyncio
async def test_a_token_total_is_comparable_in_every_period_a_per_outcome_rate_is_not(isolated_db):
    """The comparability break belongs to the per-outcome unit; a plain token total spans it."""
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_manifest(db)
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES ('u-aug','root','test',500,100,0.0,'test','2026-08-11T09:00:00Z','manifest')"
        )
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens,fresh_tokens_per_outcome", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    by_series = {name: {row["period"]: row["comparable"] for row in series["rows"] if row["series"] == name}
                 for name in ("fresh_tokens", "fresh_tokens_per_outcome")}
    assert by_series["fresh_tokens"]["2026-08-10"] is True
    assert by_series["fresh_tokens_per_outcome"]["2026-08-10"] is False


@pytest.mark.asyncio
async def test_the_token_total_starts_at_its_first_recorded_period(isolated_db):
    """No period before the first recorded one is served: no leading gap, no uncounted mark.

    Calls dated before the instrument began do not start the series either, so their
    uncounted and cached figures never reach the total's card.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    empty = await measurement_series("fresh_tokens", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert [row for row in empty["rows"] if row["series"] == "fresh_tokens"] == []

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_manifest(db)
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,total_tokens,cache_read_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES ('u-before','root','test',NULL,900,0.0,'test','2026-07-28T09:00:00Z','manifest')"
        )
        _seed_usage(db, "u-total", total=1000, prompt=600, completion=100, cache_creation=0, cache_read=300)
        db.commit()
    finally:
        db.close()

    series = await measurement_series("fresh_tokens", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    totals = [row for row in series["rows"] if row["series"] == "fresh_tokens"]
    assert totals[0]["period"] == "2026-09-21" and totals[0]["value"] == 700
    assert sum(row["unknown"] for row in totals) == 0 and sum(row["cached"] for row in totals) == 300


@pytest.mark.asyncio
async def test_first_try_misses_only_a_rejection_or_a_failed_or_changed_attempt(isolated_db):
    """A re-selection is not a second try.

    The served figure counted ``attempt_ordinal > 1``, so a node re-selected after its
    watch armed, after a hold, or after a plan pause read as a miss: production released
    attempt one 2,366 times for ``watch_armed`` and 3,217 for ``review_requested`` against
    90 ``failed`` and 400 ``requirements_changed``, and the shipped number was 0.80 where
    the work was 0.93.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=24)  # creates plan-root and 24 clean accepted tasks
        names = ("reselected", "rejected", "failed-release", "changed", "follow-up")
        for name in names:
            _seed_task(db, "plan-root", name)
        _seed_task(db, "plan-root", "clean")
        db.execute(
            "INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,requirements_revision,"
            "attempt_ordinal,provenance,selected_at,released_at,release_reason) VALUES "
            "('a1','plan-root','reselected','root',1,1,'test','2026-09-20T00:00:00','2026-09-21T00:00:00','watch_armed'),"
            "('a2','plan-root','reselected','root',1,2,'test','2026-09-21T00:00:00',NULL,NULL),"
            "('a3','plan-root','failed-release','root',1,1,'test','2026-09-20T00:00:00','2026-09-21T00:00:00','failed'),"
            "('a4','plan-root','changed','root',1,1,'test','2026-09-20T00:00:00','2026-09-21T00:00:00','requirements_changed')"
        )
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                   "VALUES ('e-rej','rejected','test','review_rejected','active','active',?)", (_WEEK,))
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
                   "commitment_id,requirements_revision,revision,created_at,updated_at) "
                   "VALUES ('gone-failed','plan-root',NULL,1,'task','Debt','done','failed','gone-failed',1,1,'t','t')")
        db.execute("UPDATE plan_nodes SET follow_up_of='gone-failed' WHERE id='follow-up'")
        db.commit()
    finally:
        db.close()

    series = await measurement_series("first_try_acceptance", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in series["rows"] if row["period"] == "2026-09-21")
    assert row["n"] == 30, row
    assert row["value"] == pytest.approx(26 / 30), row  # the 24 clean seeded ones, plus reselected and clean


@pytest.mark.asyncio
async def test_first_try_misses_a_revision_of_a_sent_back_task(isolated_db):
    """A revision accepted after its original was sent back is not a first try.

    The served figure asked only whether the settled node itself was rejected or
    released bad, or revised a *failed* node. A send-back writes ``review_rejected``
    on the node it rejects and closes it, so the revision it creates was accepted
    against a node that is closed rather than failed and counted as a first try:
    the acceptance rate read high by exactly the revisions that took two tries. The
    reworked set is checked on the node and on its immediate ``follow_up_of``
    predecessor, which is enough at every depth because each send-back marks its
    own node; a predecessor that was ordinary accepted-with-follow-up is not a miss.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=20)  # 20 clean accepted tasks (min_n for this series is 20)
        for name in ("sent-back", "revision", "handoff-revision"):
            _seed_task(db, "plan-root", name)
        # The send-back marks the node it rejects and closes it ('Sent back').
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id='sent-back'")
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                   "VALUES ('e-sent-back','sent-back','test','review_rejected','active','active',?)", (_WEEK,))
        db.execute("UPDATE plan_nodes SET follow_up_of='sent-back' WHERE id='revision'")
        # An accept-with-follow-up predecessor is not reworked: its revision is a first try.
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
                   "commitment_id,requirements_revision,revision,created_at,updated_at) "
                   "VALUES ('handoff','plan-root',NULL,1,'task','Measured task','done','accepted_with_followup',"
                   "'handoff',1,1,'t','t')")
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                   "VALUES ('event-handoff','handoff','test','accepted','active','accepted_with_followup',?)", (_WEEK,))
        db.execute("UPDATE plan_nodes SET follow_up_of='handoff' WHERE id='handoff-revision'")
        db.commit()
    finally:
        db.close()

    series = await measurement_series("first_try_acceptance", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    row = next(row for row in series["rows"] if row["period"] == "2026-09-21")
    assert row["n"] == 23, row  # the sent-back original is closed, so it is not an outcome
    assert row["value"] == pytest.approx(22 / 23), row  # 20 clean + handoff + handoff-revision


@pytest.mark.asyncio
async def test_no_series_serves_a_period_before_its_instrument_existed(isolated_db):
    """A period the product could not observe is not served at all: each series starts at its first observed period.

    It is never drawn as zero, and no leading row stands for earlier time (weeks grain here; the Days grain has its own test).

    Fail-first on the shipped registry, whose starts all sat at 2026-08-10: active hours
    began 2026-08-31, user-owned holds 2026-09-07, first-try 2026-09-14 and presence
    2026-09-29, yet every earlier period was drawn as if it had been measured. The data is
    seeded on both sides of each boundary so the only thing that can make 08-24 differ
    from 09-21 is the instrument start, and ``outcomes`` (start 2026-08-10) proves the
    clip did not overshoot into the one instrument that was already running.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    before, after = "2026-08-24", "2026-09-21"
    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_plan(db, "plan-two", "root")
        for period, first_number in ((before, 0), (after, 100)):
            settled = f"{period}T12:00:00Z"
            for number in range(first_number, first_number + 24):
                _seed_task(db, "plan-two", f"node-{number}", settled=settled)
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
                "VALUES (?, 'root', 'user', 'x', '{}', ?)", (f"msg-{period}", f"{period}T10:00:00Z"),
            )
            db.execute(
                "INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) "
                "VALUES ('steve','thread','root',?, ?, 60, 1, 'hidden')", (f"{period}T11:00:00Z", f"{period}T12:00:00Z"),
            )
        _seed_decision(db, 1, "A", raised="2026-08-25T12:00:00Z")
        _seed_decision(db, 2, "A", raised="2026-09-22T12:00:00Z")
        db.commit()
    finally:
        db.close()

    result = await measurement_series(
        "outcomes,active_hours_per_outcome,asks_per_outcome,first_try_acceptance,time_on_platform_h",
        project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER,
    )
    served = {(row["series"], row["period"]): row for row in result["rows"]}

    def answer(metric: str, period: str):
        row = served[(metric, period)]
        return (row["value"], row["n"])

    assert answer("outcomes", before)[0] == 24, "outcomes ran before 08-31, so its own start still serves it"
    for metric in ("active_hours_per_outcome", "asks_per_outcome", "first_try_acceptance"):
        assert (metric, before) not in served, f"{metric} serves no period before its instrument existed"
        assert answer(metric, after)[0] is not None, metric
    assert ("time_on_platform_h", before) not in served
    assert ("time_on_platform_h", after) not in served, (
        "presence started 2026-09-29: the seeded 09-21 visit predates it, so that period is not served and is never a measured hour"
    )


@pytest.mark.asyncio
async def test_key_figures_rejects_unknown_iana_timezone():
    from api.measurement_recent import measurement_key_figures

    with pytest.raises(HTTPException) as error:
        await measurement_key_figures(project="all", agent="none", timezone_name="not/a-timezone", user=USER)
    assert error.value.status_code == 422








@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["Mars/Olympus", "PST", "", "../etc/passwd", "UTC+8"])
async def test_key_figures_rejects_non_iana_zones(zone):
    from api.measurement_recent import measurement_key_figures

    with pytest.raises(HTTPException) as error:
        await measurement_key_figures(project="all", agent="none", timezone_name=zone, user=USER)
    assert error.value.status_code == 422



def _seed_pick_any(db, index: int, chosen: list[str], recommendation: list[str], note: str | None = None) -> None:
    decision = {
        "thread_id": "root", "raised_at": "2026-09-15T12:00:00Z", "answered_at": "2026-09-23T12:00:00Z",
        "state": "answered", "pick": "any", "recommendation": recommendation,
        "answer": {"option_ids": chosen, "text": note},
    }
    db.execute(
        "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
        "VALUES (?, 'local', 'plan-decision', ?, ?, 't', 't')",
        (f"any-{index}", f"any-{index}", json.dumps({"plan_decision": decision})),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("chosen,recommendation,note,expected", [
    (["A", "B"], ["A", "B"], None, (1, 0, 0)),
    (["B", "A"], ["A", "B"], None, (1, 0, 0)),  # click order never matters
    (["A", "B"], ["A", "B"], "later", (1, 0, 0)),  # the note is ignored when the set matches
    ([], [], None, (1, 0, 0)),  # recommending none and choosing none is taking it
    ([], ["A"], "do it myself", (0, 1, 0)),  # none chosen, a note, something was recommended
    ([], ["A"], None, (0, 0, 1)),  # none chosen and no note is a different answer
    ([], [], "a note", (1, 0, 0)),  # sets match, and the note is ignored: still taken
    (["A"], ["A", "B"], None, (0, 0, 1)),
    (["C"], ["A"], "also this", (0, 0, 1)),
])
async def test_pick_any_answers_land_in_exactly_one_alignment_bucket(isolated_db, chosen, recommendation, note, expected):
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=20)
        for index in range(10):
            _seed_pick_any(db, index, chosen, recommendation, note)
        db.commit()
    finally:
        db.close()
    result = await measurement_series(",".join(ALIGNMENT_IDS), project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    rows = {row["series"]: row for row in result["rows"] if row["period"] == WEEK}
    assert tuple(rows[metric]["value"] for metric in ALIGNMENT_IDS) == expected
    assert {row["n"] for row in rows.values()} == {10}


@pytest.mark.asyncio
async def test_a_period_mixing_pick_one_and_pick_any_sums_to_the_answered_count(isolated_db):
    from api.measurement_queries import prepare_outcome_periods, query_family
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_outcomes(db, "root", count=20)
        # Pick one: took it, other, neither.
        _seed_decision(db, 0, "B")
        _seed_decision(db, 1, "other")
        _seed_decision(db, 2, "A")
        # Pick any: took it, other, neither, neither.
        _seed_pick_any(db, 0, ["A", "B"], ["B", "A"])
        _seed_pick_any(db, 1, [], ["A"], "mine")
        _seed_pick_any(db, 2, ["C"], ["A"])
        _seed_pick_any(db, 3, [], ["A"])
        db.commit()
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
                  "from": WEEK, "to": WEEK, "max_active_gap_seconds": 600}
        prepare_outcome_periods(db, _grained(params))
        (row,) = [item for item in query_family(db, "decisions", _grained(params)) if item["period"] == WEEK]
    finally:
        db.close()
    assert row["answered"] == 7
    assert (row["recommendation"], row["other"], row["neither"]) == (2, 2, 3)
    assert row["recommendation"] + row["other"] + row["neither"] == row["answered"]
