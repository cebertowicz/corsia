"""The Agents surface: metric independence, honest empties, and own-baseline findings.

Red for every rule below until ``GET /api/measurement/agents`` serves the table:
an automatic finding may not move when the reader changes column, an empty scope
may not read as "nothing needs a look", and a period whose cost was never recorded
may not read as free.

The ids and thresholds here are the ones ``REGISTRY`` currently serves the
panels, so a column and the panel below it cannot measure different things.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from tests.plan_writer_authorizer import raw_plan_seed

USER = {"id": "steve"}

# Two named calendars. The Agents findings frame is the last nine *completed* periods, so which
# periods a served answer refers to is a fact about today's date, and a proof that reads
# the live clock would pass until the frame slides past 2026-08-31 and then fail for a
# reason no reader can act on. PRE is a day whose frame straddles the change in what an
# outcome counts; POST is a day whose frame sits wholly after it. Nothing here overrides
# production: only the test names the clock, through the seam the owner already has.
PRE_BREAK = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
POST_BREAK = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _freeze(monkeypatch, instant: datetime) -> datetime:
    """Bind the clock this surface reads, at the owner's own seam.

    The route resolves one ``Grain`` per request, and ``resolve_grain`` in
    ``core.measurement_scope`` defaults its clock there (``api.measurement_scope`` is only the
    HTTP facade). Nothing in production takes a date -- the served route passes no ``now`` --
    and only these proofs name a calendar.
    """
    from core import measurement_scope

    class _NamedClock(measurement_scope.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None or instant.tzinfo is not None else instant.replace(tzinfo=tz)

    monkeypatch.setattr(measurement_scope, "datetime", _NamedClock)
    return instant


@pytest.mark.asyncio
async def test_one_request_grain_feeds_the_prepared_window_and_the_frame(isolated_db, monkeypatch):
    """One ``Grain`` is resolved per request, so a Monday boundary cannot split it.

    A clock that jumps a whole period between reads is exactly the failure this guards. If the
    route and ``build_agents`` each derived their own periods, the newest period of the frame
    could land outside the window prepared for it and the current period would silently answer
    as a missing point. The prepared params and the build must hold the very same ``Grain``.
    """
    from datetime import timedelta

    from api import measurement
    from core import measurement_scope

    instants = iter([PRE_BREAK, PRE_BREAK + timedelta(days=7)])

    class _AdvancingClock(measurement_scope.datetime):
        @classmethod
        def now(cls, tz=None):
            value = next(instants)
            return value if tz is None or value.tzinfo is not None else value.replace(tzinfo=tz)

    prepared: list[dict] = []
    handed: list = []
    real_prepare = measurement.prepare_outcome_periods
    real_build = measurement.build_agents

    def spy_prepare(db, params, *, roots=False):
        prepared.append(dict(params))
        return real_prepare(db, params, roots=roots)

    def spy_build(db, params, registry, metric, sort, grain, *args, **kwargs):
        handed.append(grain)
        return real_build(db, params, registry, metric, sort, grain, *args, **kwargs)

    monkeypatch.setattr(measurement_scope, "datetime", _AdvancingClock)
    monkeypatch.setattr(measurement, "prepare_outcome_periods", spy_prepare)
    monkeypatch.setattr(measurement, "build_agents", spy_build)

    result = await _read()

    assert len(prepared) == 1, prepared
    assert len(handed) == 1, handed
    assert prepared[0]["grain"] is handed[0], "the prepared window and the build must share one Grain"
    # The one read named the first instant: the period in progress is PRE_BREAK's.
    assert handed[0].current == "2026-09-28"
    assert result["period"]["current"] == "2026-09-28"


@pytest.mark.asyncio
async def test_a_registry_that_stops_serving_an_id_is_not_the_callers_mistake(isolated_db, monkeypatch):
    """EAS11: an internal fault must not arrive as a 400 with its internal text echoed.

    Only the caller's own parameters are a 400 here. If the registry stops serving an
    id this surface needs, that is this server's failure, and a blanket conversion of
    every ValueError into a client error would hide it as a bad request.
    """
    from api import measurement

    broken = dict(measurement.REGISTRY)
    broken.pop("asks_per_outcome")
    monkeypatch.setattr(measurement, "REGISTRY", broken)
    with pytest.raises(ValueError, match="registry no longer serves"):
        await _read()


@pytest.mark.asyncio
async def test_an_unknown_column_or_order_is_refused_before_any_scope_lookup_or_preparation(isolated_db, monkeypatch):
    """The caller's error is answered up front, not after the expensive work.

    A rejected metric or sort used to be noticed only inside ``build_agents``, after
    ``validate_scope`` and the whole prepared window had already run.
    """
    from api import measurement

    ran: list[str] = []
    real_prepare = measurement.prepare_outcome_periods
    real_scope = measurement.validate_scope

    def spy_prepare(db, params, *, roots=False):
        ran.append("prepare")
        return real_prepare(db, params, roots=roots)

    def spy_scope(db, user_id, project, agent):
        ran.append("scope")
        return real_scope(db, user_id, project, agent)

    monkeypatch.setattr(measurement, "prepare_outcome_periods", spy_prepare)
    monkeypatch.setattr(measurement, "validate_scope", spy_scope)

    for kwargs in ({"metric": "tokens_per_outcome"}, {"sort": "cheapest"}):
        with pytest.raises(HTTPException) as refused:
            await _read(**kwargs)
        assert refused.value.status_code == 400, kwargs
    assert ran == [], ran


def test_the_two_named_calendars_sit_either_side_of_the_bound():
    """The proofs below are only meaningful if these two days really differ."""
    pre, post = _frame(9, now=PRE_BREAK), _frame(9, now=POST_BREAK)
    assert pre[0] < "2026-08-31" <= post[0], (pre[0], post[0])
    assert pre[-1] >= "2026-08-31", "PRE must straddle the bound, not sit wholly before it"
    assert post[-1] > post[0] and post[0] >= "2026-08-31"


def _frame(count: int = 12, now=None) -> list[str]:
    """Return the last ``count`` completed UTC weeks the page grain draws, oldest first."""
    from api.measurement_scope import resolve_grain

    return list(resolve_grain("weeks", "UTC", now=now).periods)[-count:]


def _seed_project(db, project_id: str, name: str | None = None) -> None:
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
               (project_id, name or project_id, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"))


def _seed_agent(db, agent_id: str, *, project_id: str | None = None, enabled: bool = True,
                status: str | None = "active") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,"
        "agent_responsibility,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (agent_id, "steve", agent_id, project_id, None, int(enabled), status,
         "Owns measurement" if enabled else None, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"))
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
               "VALUES (?,'thread',?,'active',1,'t','t')", (f"plan-{agent_id}", agent_id))


def _seed_child(db, parent_id: str, child_id: str, *, project_id: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,"
        "agent_responsibility,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (child_id, "steve", child_id, project_id, parent_id, 0, "active", None,
         "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"))


def _seed_period(db, agent_id: str, period: str, outcomes: int, *, not_first_try: int = 0,
               difficulty: str = "standard", start: int = 0, at: str | None = None) -> None:
    """Settle ``outcomes`` task nodes in one completed period, numbered from ``start``.

    ``week`` is a period key (a Monday, or a day on the Days grain) and the nodes settle at its
    UTC noon, unless ``at`` names the exact UTC instant they settle at."""
    rows = [(f"{agent_id}-{period}-{index}", f"plan-{agent_id}", index, f"commitment-{agent_id}-{period}-{index}",
             difficulty, at or f"{period}T12:00:00Z") for index in range(start, start + outcomes)]
    with raw_plan_seed(db):
        db.executemany(
            "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
            "commitment_id,requirements_revision,revision,difficulty,created_at,updated_at) "
            "VALUES (?, ?, NULL, ?, 'task', 'Measured task', 'done', 'accepted', ?, 1, 1, ?, ?, ?)",
            [(node_id, plan_id, order_key, commitment_id, node_difficulty, settled, settled)
             for node_id, plan_id, order_key, commitment_id, node_difficulty, settled in rows],
        )
        db.executemany(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES (?,?,'test','accepted','active','accepted',?)",
            [(f"event-{node_id}", node_id, settled) for node_id, _, _, _, _, settled in rows],
        )
        db.executemany(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES (?,?,'test','review_rejected','active','active',?)",
            [(f"reject-{node_id}", node_id, settled) for node_id, _, _, _, _, settled in rows[:not_first_try]],
        )


def _seed_tokens(db, thread_id: str, period: str, tokens: int, *, count: int = 1) -> None:
    db.executemany(
        "INSERT INTO llm_usage (id,thread_id,model,total_tokens,created_at) VALUES (?,?,'test-model',?,?)",
        [(f"usage-{thread_id}-{period}-{index}", thread_id, tokens // count, f"{period}T13:00:00Z")
         for index in range(count)],
    )


def _seed_hold(db, agent_id: str, period: str, node_index: int = 0) -> None:
    node_id = f"{agent_id}-{period}-{node_index}"
    db.execute(
        "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at) "
        "VALUES (?,?,'blocked','ask','User hold','user','test',?)",
        (f"hold-{node_id}", node_id, f"{period}T14:00:00Z"),
    )


def _seed_decision(db, agent_id: str, period: str, index: int) -> None:
    metadata = {"plan_decision": {"thread_id": agent_id, "raised_at": f"{period}T15:00:00Z",
                                  "state": "open"}}
    db.execute("INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
               "VALUES (?, 'local', 'plan-decision', ?, ?, 't', 't')",
               (f"decision-{agent_id}-{period}-{index}", f"decision-{agent_id}-{period}-{index}", json.dumps(metadata)))


def _seeded(db_seed):
    """Seed one scenario once, so a test can read the same state through several requests."""
    from infra.database import get_db

    db = get_db()
    try:
        db_seed(db)
        db.commit()
    finally:
        db.close()


async def _agents(db_seed, **kwargs):
    """Seed then read one Agents response through the served route."""
    _seeded(db_seed)
    return await _read(**kwargs)


async def _read(**kwargs):
    from api.measurement import measurement_agents

    return await measurement_agents(
        **{"project": "all", "agent": "none", "timezone_name": "UTC", "grain": "weeks",
           "sort": "ranked", "metric": "fresh_tokens_per_outcome", **kwargs}, user=USER)


def _comparable(metric, period):
    """Isolate a statistic from the calendar bound, which has its own boundary proof."""
    return True


def _always_measured(metric: str, period: str) -> bool:
    """Isolate a statistic from the instrument calendar, which has its own boundary proof."""
    return True


def _build(db_seed, *, metric="fresh_tokens_per_outcome", sort="ranked", comparable=None, measured=None,
           now=None, kind="weeks", tz="UTC"):
    """Run the same build the served route runs, with the route's registry and its two period predicates."""
    from api.measurement import REGISTRY, period_comparable, period_measured
    from api.measurement_agents import build_agents
    from api.measurement_findings import derive_findings_state
    from api.measurement_queries import prepare_outcome_periods
    from api.measurement_scope import resolve_grain
    from infra.database import get_db

    if db_seed:
        _seeded(db_seed)
    grain = resolve_grain(kind, tz, now=now)
    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None}
    db = get_db()
    try:
        prepare_outcome_periods(db, {**params, "grain": grain}, roots=True)
        rows, findings, summaries, count = build_agents(
            db, params, REGISTRY, metric, sort, grain, comparable or period_comparable,
            measured or (lambda name, period: period_measured(name, period, grain)))
        return {"rows": rows, "findings": findings, "project_summaries": summaries["active"],
                "agent_count": count, "findings_state": derive_findings_state(rows, findings, count)}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_an_agent_with_no_history_is_present_and_too_few(isolated_db):
    from api.measurement_agents import RANKING_MINIMUM_N

    def seed(db):
        _seed_agent(db, "idle", project_id="p1")
        _seed_project(db, "p1")

    result = await _agents(seed)
    assert [row["agent"]["name"] for row in result["rows"]] == ["idle"]
    row = result["rows"][0]
    assert row["status"] == "too_few" and row["rank"] is None and row["n"] == 0
    # The registry says a count needs no minimum: an agent that settled nothing
    # measured zero outcomes. Its cost is the opposite case, and it is null:
    # nothing was recorded, which is not the same as having spent nothing.
    assert row["metrics"]["outcomes"]["value"] == pytest.approx(0.0)
    assert row["metrics"]["outcomes"]["minimum_n"] == 0
    assert row["metrics"]["fresh_tokens_per_outcome"]["value"] is None
    assert row["metrics"]["first_try_acceptance"]["value"] is None
    assert row["metrics"]["asks_per_outcome"]["value"] is None
    assert row["metrics"]["fresh_tokens_per_outcome"]["minimum_n"] == RANKING_MINIMUM_N
    assert row["difficulty_mix"] == {}
    # An agent that answered nothing is not a healthy population either.
    assert result["findings_state"] == "too_little_history"


@pytest.mark.asyncio
async def test_a_scope_with_no_active_agents_is_its_own_empty_state(isolated_db):
    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "retired", project_id="p1", enabled=False)

    result = await _agents(seed)
    assert result["findings_state"] == "no_agents"
    assert result["rows"] == [] and result["findings"] == []
    assert result["project_summaries"] == []
    # Distinct from a populated scope that simply cannot be compared yet.
    assert result["findings_state"] != "too_little_history"


@pytest.mark.asyncio
@pytest.mark.parametrize("clock,expected", ((PRE_BREAK, "too_little_history"), (POST_BREAK, "flagged")))
async def test_the_selected_column_and_order_cannot_change_what_the_findings_found(
        isolated_db, monkeypatch, clock, expected):
    _freeze(monkeypatch, clock)
    frame = _frame()[-9:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for index, period in enumerate(frame):
            _seed_period(db, "worker", period, 20)
            _seed_tokens(db, "worker", period, 2000 if index < 8 else 8000)

    _seeded(seed)
    # Served path: whichever column the reader selects, and whichever order they pick, the
    # automatic findings answer one thing, and it is the same thing on either calendar.
    # Which of the two answers the route gives is a fact about the frame, not about the
    # selection, so both halves are proven against named clocks.
    states = {(await _read(metric=metric))["findings_state"]
              for metric in ("outcomes", "fresh_tokens_per_outcome", "first_try_acceptance", "asks_per_outcome")}
    orders = {(await _read(sort=name))["findings_state"] for name in ("ranked", "worsening")}
    assert states == orders == {expected}
    # With comparable periods in the frame, the finding is still one finding:
    # the selected column cannot create, remove or rename what was found.
    flagged = {(result["findings_state"], tuple(item["metric"] for item in result["findings"]))
               for result in (_build(None, metric=metric, comparable=_comparable, measured=_always_measured)
                              for metric in ("outcomes", "fresh_tokens_per_outcome",
                                             "first_try_acceptance", "asks_per_outcome"))}
    assert flagged == {("flagged", ("fresh_tokens_per_outcome",))}


@pytest.mark.asyncio
async def test_a_period_that_recorded_no_cost_is_not_a_free_period(isolated_db, monkeypatch):
    # The served answer this row compares against is the frame it was built on, so the
    # calendar is named rather than inherited from the day the test runs. This instant's
    # trailing periods all sit inside every instrument's own window, so the only thing the
    # row is missing is the one period seeded without cost.
    _freeze(monkeypatch, POST_BREAK)
    periods = _frame(now=POST_BREAK)
    trailing = periods[-4:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for index, period in enumerate(trailing):
            _seed_period(db, "worker", period, 40)
            if index < 3:
                _seed_tokens(db, "worker", period, 4000)

    result = await _agents(seed)
    row = result["rows"][0]
    # Three measured periods and one unmeasured: the pooled cost would divide
    # outcomes whose spend is unknown, so the cell says unmeasured.
    assert row["metrics"]["fresh_tokens_per_outcome"]["value"] is None
    assert row["metrics"]["first_try_acceptance"]["value"] == pytest.approx(1.0)
    assert row["metrics"]["outcomes"]["value"] == pytest.approx(40.0)
    assert [point["period"] for point in row["metrics"]["outcomes"]["periods"]] == trailing
    assert row["metrics"]["fresh_tokens_per_outcome"]["previous_n"] == 0
    assert row["metrics"]["fresh_tokens_per_outcome"]["periods"][-1]["value"] is None
    assert row["metrics"]["fresh_tokens_per_outcome"]["periods"][0]["value"] == pytest.approx(100)
    assert result["findings_state"] == "too_little_history"


@pytest.mark.asyncio
async def test_descendant_threads_attribute_to_the_agent_that_owns_them(isolated_db):
    periods = _frame()
    trailing = periods[-8:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        _seed_child(db, "worker", "worker-child", project_id="p1")
        for period in trailing:
            _seed_period(db, "worker", period, 20)
            _seed_tokens(db, "worker-child", period, 1000)
            _seed_hold(db, "worker", period)

    result = await _agents(seed)
    row = result["rows"][0]
    assert row["metrics"]["fresh_tokens_per_outcome"]["value"] == pytest.approx(1000 / 20)
    assert row["metrics"]["asks_per_outcome"]["value"] == pytest.approx(1 / 20)
    assert result["project_summaries"][0]["project"]["id"] == "p1"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,latest_size", (("weeks", 4), ("days", 7)), ids=("weeks", "days"))
async def test_the_project_summary_follows_the_latest_window_while_the_frame_stays(
    isolated_db, monkeypatch, kind, latest_size
):
    """The project summary counts the grain's latest window, never the older periods of the range.

    The grain draws more periods than the table pools (12 weeks or 14 days); an outcome in
    the previous window is inside the page's range but outside the window every list answers
    for. The caption and ``period`` echoed by the response name that window, and the project
    summary counts exactly it. The clock is named, so no live date decides the windows.
    """
    from api.measurement_agents import _project_summaries
    from api.measurement_queries import prepare_outcome_periods
    from api.measurement_scope import resolve_grain
    from infra.database import get_db

    _freeze(monkeypatch, POST_BREAK)
    grain = resolve_grain(kind, "UTC", now=POST_BREAK)
    latest = list(grain.latest.periods)
    # The previous window is in the range (and the table compares against it) but is not the
    # window the project summary counts; on Days the range holds nothing older.
    far = grain.previous.periods[0]
    assert far not in latest and far in grain.periods
    seeded = {far: 30, **{period: 20 for period in latest}}

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for period, outcomes in seeded.items():
            _seed_period(db, "worker", period, outcomes)

    _seeded(seed)
    served = await _read(grain=kind)
    assert served["caption"] == f"Last {latest_size} completed {kind}"
    assert served["period"]["grain"] == kind
    assert served["period"]["latest"] == {"from": latest[0], "to": grain.period_end(latest[-1])}
    # The count holds exactly the latest window's seeded periods: the far one is in the
    # range but not in the window, so it must not enter the project's outcomes.
    assert [item["outcomes"] for item in served["project_summaries"]] == [20 * latest_size]
    assert served["rows"][0]["n"] == 20 * latest_size

    db = get_db()
    try:
        prepare_outcome_periods(db, {"user_id": "steve", "scope_project": "all", "scope_agent": None, "grain": grain},
                              roots=True)
        counted = _project_summaries(db, served["rows"], grain)
    finally:
        db.close()
    assert served["project_summaries"] == counted["active"]
    assert served["inactive_projects"] == counted["inactive"] == []


@pytest.mark.asyncio
async def test_the_per_agent_grain_sums_to_the_series_the_panels_serve(isolated_db):
    """Root grouping is one query owner's second grain, never a second measurement."""
    from api.measurement import measurement_series
    from api.measurement_queries import PREPARED_ROOT_OUTCOMES, prepare_outcome_periods, query_family
    from api.measurement_scope import resolve_grain
    from infra.database import get_db

    grain = resolve_grain("weeks", "UTC")
    trailing = list(grain.periods)[-8:]

    def scenario(db):
        _seed_project(db, "p1")
        for name in ("one", "two"):
            _seed_agent(db, name, project_id="p1")
        for index, period in enumerate(trailing):
            _seed_period(db, "one", period, 20 + index)
            _seed_period(db, "two", period, 5)
            _seed_tokens(db, "one", period, 100 + index)
            _seed_tokens(db, "two", period, 50)

    _seeded(scenario)
    db = get_db()
    try:
        params = {"user_id": "steve", "scope_project": "all", "scope_agent": None, "grain": grain}
        prepare_outcome_periods(db, params, roots=True)
        scope_outcomes = {row["period"]: row["outcomes"] for row in query_family(db, "outcomes", params)}
        per_root: dict[str, dict[str, int]] = {}
        for row in db.execute(f"SELECT root_id,period,outcomes FROM {PREPARED_ROOT_OUTCOMES}").fetchall():
            per_root.setdefault(row["period"], {})[row["root_id"]] = row["outcomes"]
        assert sorted(per_root) == sorted(scope_outcomes)
        assert all(sum(counts.values()) == scope_outcomes[period] for period, counts in per_root.items())
        assert (sum(row["tokens"] for row in query_family(db, "tokens", params, grouping="root"))
                == sum(row["tokens"] for row in query_family(db, "tokens", params)))
        assert (sum(row["user_holds"] for row in query_family(db, "decisions", params, grouping="root"))
                == sum(row["user_holds"] for row in query_family(db, "decisions", params)))
    finally:
        db.close()

    series = await measurement_series("outcomes", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    agents = await _read()
    # The table pools the grain's latest window; the panel's series covers every period.
    assert sum(row["n"] for row in agents["rows"]) == sum(row["n"] for row in series["rows"]
                                                          if row["period"] in grain.latest.periods) > 0


@pytest.mark.asyncio
async def test_the_finding_needs_strictly_more_than_two_deviations(isolated_db):
    """A baseline of 100..107 puts the threshold at 103.5 + 2*sqrt(6) ≈ 108.399."""
    frame = _frame()[-9:]
    baseline = [100, 101, 102, 103, 104, 105, 106, 107]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "just-over", project_id="p1")
        _seed_agent(db, "just-under", project_id="p1")
        for agent_id, current_tokens in (("just-over", 2168), ("just-under", 2167)):
            for index, period in enumerate(frame):
                _seed_period(db, agent_id, period, 20)
                _seed_tokens(db, agent_id, period, baseline[index] * 20 if index < 8 else current_tokens)

    result = _build(seed, comparable=_comparable, measured=_always_measured)
    by_agent = {row["agent"]["name"]: row for row in result["rows"]}
    # 108.40 per outcome clears the bar; 108.35 does not. Two deviations is not
    # yet a finding: the rule is strict.
    assert by_agent["just-over"]["flags"]["fresh_tokens_per_outcome"]["flagged"] is True
    assert by_agent["just-under"]["flags"]["fresh_tokens_per_outcome"]["flagged"] is False
    assert {item["agent"]["name"] for item in result["findings"]} == {"just-over"}
    assert result["findings_state"] == "flagged"
    rule = by_agent["just-over"]["flags"]["fresh_tokens_per_outcome"]
    assert rule["eligible"]
    assert rule["adverse_size"] == pytest.approx((108.4 - 103.5) / statistics.stdev(baseline), rel=1e-9)
    assert rule["mean"] == pytest.approx(103.5)
    assert not rule["zero_variance_baseline"]


@pytest.mark.asyncio
async def test_a_thin_period_in_the_baseline_makes_the_metric_uncomparable(isolated_db):
    periods = _frame()
    frame = periods[-9:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for index, period in enumerate(frame):
            _seed_period(db, "worker", period, 20 if index != 3 else 6)
            _seed_tokens(db, "worker", period, 2000 if index != 3 else 6000)

    result = await _agents(seed)
    rule = result["rows"][0]["flags"]["fresh_tokens_per_outcome"]
    assert rule["eligible"] is False and rule["reason"] == "too_little_history"
    assert rule["minimum_n"] == 20
    assert result["findings"] == []
    assert result["findings_state"] == "too_little_history"


@pytest.mark.asyncio
async def test_a_flat_baseline_reports_no_standardized_size_but_a_relative_move(isolated_db):
    periods = _frame()
    frame = periods[-9:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for index, period in enumerate(frame):
            _seed_period(db, "worker", period, 20)
            _seed_tokens(db, "worker", period, 2000 if index < 8 else 2400)

    result = _build(seed, comparable=_comparable, measured=_always_measured)
    finding = result["findings"][0]
    assert finding["standardized_size"] is None
    assert finding["zero_variance_baseline"] is True
    assert finding["relative_adverse_change"] == pytest.approx(0.2)
    assert finding["sd"] == 0
    assert finding["metric"] == "fresh_tokens_per_outcome"
    assert json.dumps(result).count("Infinity") == 0 and "NaN" not in json.dumps(result)


@pytest.mark.asyncio
async def test_first_try_acceptance_worsening_is_flagged_on_its_own_direction(isolated_db):
    periods = _frame()
    frame = periods[-9:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for index, period in enumerate(frame):
            # A perfect first-try history, then four rejected-attempt tasks.
            _seed_period(db, "worker", period, 25, not_first_try=0 if index < 8 else 10)
            _seed_tokens(db, "worker", period, 2500)

    result = _build(seed, comparable=_comparable, measured=_always_measured)
    finding = result["findings"][0]
    assert finding["metric"] == "first_try_acceptance"
    assert finding["current"] == pytest.approx(0.6) and finding["mean"] == pytest.approx(1.0)
    assert finding["action"] == "Review rejected/re-attempted tasks."
    assert result["findings_state"] == "flagged"


@pytest.mark.asyncio
async def test_findings_are_capped_at_five_and_ordered_by_severity(isolated_db):
    frame = _frame()[-9:]

    def seed(db):
        _seed_project(db, "p1")
        for position, name in enumerate(("a", "b", "c", "d", "e", "f", "g")):
            _seed_agent(db, name, project_id="p1")
            for index, period in enumerate(frame):
                _seed_period(db, name, period, 20)
                # A flat baseline each, then a move that grows with position.
                _seed_tokens(db, name, period, 2000 if index < 8 else 2000 + 400 * position)

    result = _build(seed, comparable=_comparable, measured=_always_measured)
    assert len(result["findings"]) == 5
    assert [item["relative_adverse_change"] for item in result["findings"]] == \
        sorted((item["relative_adverse_change"] for item in result["findings"]), reverse=True)
    assert result["findings"][0]["agent"]["name"] == "g"
    assert {item["action"] for item in result["findings"]} == {"Review its 5 costliest recent tasks."}


@pytest.mark.asyncio
async def test_no_task_level_identifier_leaves_the_agents_route(isolated_db, monkeypatch):
    # Named, not inherited: this frame sits wholly inside the outcome instrument's own
    # window, so the mix and the count are the same outcomes and cannot drift with the
    # day the test runs.
    _freeze(monkeypatch, POST_BREAK)
    frame = _frame(now=POST_BREAK)[-9:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for period in frame:
            _seed_period(db, "worker", period, 20, difficulty="hard")
            _seed_tokens(db, "worker", period, 2000)

    result = await _agents(seed)
    body = json.dumps(result)
    for leaked in ("plan-", "commitment-", "usage-", "attempt-"):
        assert leaked not in body
    assert result["rows"][0]["difficulty_mix"] == {"hard": 80}
    # The mix the table shows is the count the table shows, bucket for bucket: a rank
    # may not look explainable while part of the work is missing from the explanation.
    assert sum(result["rows"][0]["difficulty_mix"].values()) == result["rows"][0]["n"]


@pytest.mark.asyncio
async def test_work_that_was_never_classified_still_shows_up_in_the_mix(isolated_db, monkeypatch):
    """A node with no difficulty is work, and the visible mix may not silently drop it."""
    _freeze(monkeypatch, POST_BREAK)
    trailing = _frame(now=POST_BREAK)[-8:]

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for period in trailing:
            _seed_period(db, "worker", period, 12, difficulty="standard")
        # Only the latest four periods are pooled into the row; the older four are outside it.
        for period in trailing[4:]:
            # Difficulty is a nullable column, so these outcomes have no bucket until
            # the query files them somewhere honest.
            _seed_period(db, "worker", period, 8, difficulty=None, start=100)

    result = await _agents(seed)
    row = result["rows"][0]
    assert row["difficulty_mix"] == {"standard": 48, "unclassified": 32}
    assert sum(row["difficulty_mix"].values()) == row["n"] == 80


@pytest.mark.asyncio
async def test_an_unknown_metric_or_sort_is_a_bad_request(isolated_db):
    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")

    _seeded(seed)
    for kwargs in ({"metric": "outcomes_per_period"}, {"metric": "tokens_per_outcome"}, {"sort": "cheapest"}):
        with pytest.raises(HTTPException) as refused:
            await _read(**kwargs)
        assert refused.value.status_code == 400


@pytest.mark.asyncio
async def test_the_agents_route_answers_over_http_with_the_required_composite_scope(isolated_db):
    from fastapi.testclient import TestClient

    from auth_fixtures import OWNER_TOKEN
    from main import app

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
    assert client.get("/api/measurement/agents", headers=headers).status_code == 422
    assert client.get("/api/measurement/agents?project=all", headers=headers).status_code == 422
    # The viewer's timezone is required too: the periods are local days and periods.
    assert client.get("/api/measurement/agents?project=all&agent=none", headers=headers).status_code == 422
    bad_zone = client.get("/api/measurement/agents?project=all&agent=none&timezone=Mars/Base", headers=headers)
    assert bad_zone.status_code == 422, bad_zone.text
    response = client.get("/api/measurement/agents?project=all&agent=none&timezone=UTC", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["findings_state"] == "no_agents"
    assert response.json()["period"]["grain"] == "days"


@pytest.mark.asyncio
async def test_a_finding_never_compares_across_the_period_the_outcome_unit_changed(isolated_db):
    """The §Flag rule: the current period and all eight baseline points must be comparable.

    Plenty of outcomes and a fourfold cost move are not enough. Under the bound the
    series already marks rows with, the honest served answer is too little history,
    and a surface that compared across it would report a change in counting as a
    change in the work.
    """
    straddle = _frame(9, now=PRE_BREAK)
    later = _frame(9, now=POST_BREAK)

    def seed(db):
        _seed_project(db, "p1")
        for agent_id, periods in (("straddle", straddle), ("comparable", later)):
            _seed_agent(db, agent_id, project_id="p1")
            for index, period in enumerate(periods):
                _seed_period(db, agent_id, period, 20)
                _seed_tokens(db, agent_id, period, 2000 if index < 8 else 8000)

    assert straddle[0] < "2026-08-31" <= later[0], "the two frames must sit either side of the bound"

    blocked = _build(seed, now=PRE_BREAK, measured=_always_measured)
    straddle = next(row for row in blocked["rows"] if row["agent"]["id"] == "straddle")
    rule = straddle["flags"]["fresh_tokens_per_outcome"]
    assert rule["eligible"] is False and rule["reason"] == "too_little_history"
    assert blocked["findings"] == [] and blocked["findings_state"] == "too_little_history"
    # The pooled cells still answer: the bound gates comparison, not measurement.
    # Latest four periods = three at 2000 tokens plus the moved period at 8000, over 80 outcomes.
    assert straddle["metrics"]["fresh_tokens_per_outcome"]["value"] == pytest.approx(175.0)

    # The same shape of data with nine comparable periods does flag: the bound was
    # the only thing holding the finding back.
    flagged = _build(None, now=POST_BREAK, measured=_always_measured)
    assert [item["metric"] for item in flagged["findings"]] == ["fresh_tokens_per_outcome"]
    assert flagged["findings"][0]["agent"]["id"] == "comparable"
    assert flagged["findings_state"] == "flagged"
    assert next(row for row in flagged["rows"]
                if row["agent"]["id"] == "comparable")["flags"]["fresh_tokens_per_outcome"]["eligible"] is True


@pytest.mark.asyncio
async def test_the_agents_table_blanks_the_periods_its_instrument_did_not_observe(isolated_db, monkeypatch):
    """One instrument calendar serves both surfaces, at the same periods.

    Fail-first on the Agents route before this rule existed: the panels retired a period the
    metric's instrument had not started in, while the table counted that period in its row,
    its pooled cell, its difficulty mix and its baseline. The two surfaces then answered
    for different populations of the same agent, and a finding could be raised across a
    period that was never observed. The calendar is named so the retired periods are these,
    whatever day the test runs.
    """
    from api.measurement import REGISTRY, measurement_series

    _freeze(monkeypatch, PRE_BREAK)
    periods = _frame(now=PRE_BREAK)[-4:]
    first_try_start = REGISTRY["first_try_acceptance"].start

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for period in periods:
            _seed_period(db, "worker", period, 40, not_first_try=0 if period >= first_try_start else 5)
            _seed_tokens(db, "worker", period, 4000)

    result = await _agents(seed)
    points = result["rows"][0]["metrics"]["first_try_acceptance"]["periods"]
    assert [point["period"] for point in points] == periods
    for point in points:
        if point["period"] < first_try_start:
            assert (point["value"], point["n"]) == (None, 0), point["period"]
        else:
            assert point["value"] is not None and point["n"] == 40, point["period"]
    assert result["rows"][0]["metrics"]["first_try_acceptance"]["n"] == sum(
        40 for period in periods if period >= first_try_start)
    rule = result["rows"][0]["flags"]["first_try_acceptance"]
    assert (rule["eligible"], rule["reason"]) == (False, "too_little_history"), rule

    series = await measurement_series("first_try_acceptance", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    served = {row["period"]: row for row in series["rows"]}
    for point in points:
        if point["period"] < first_try_start:
            assert point["period"] not in served, "the series starts at the instrument's start, as the table's blanks do"
            continue
        assert (served[point["period"]]["value"], served[point["period"]]["n"]) == (point["value"], point["n"]), point["period"]


def _outcome_counts(row: dict) -> dict[str, int]:
    """The agent's per-period outcome counts, keyed by the period's first local date."""
    return {point["period"]: point["n"] for point in row["metrics"]["outcomes"]["periods"]}


@pytest.mark.asyncio
async def test_the_days_grain_pools_the_latest_seven_days_and_carries_the_seven_before(isolated_db, monkeypatch):
    """On Days the table's window is seven completed days, and the previous seven ride beside it.

    The day still in progress and every day older than the previous window are in neither.
    """
    _freeze(monkeypatch, POST_BREAK)  # a Monday: the day in progress is 2026-11-16
    seeded = {
        "2026-11-16": 50,  # in progress: in no window
        "2026-11-15": 8,   # last day of the latest window
        "2026-11-09": 12,  # first day of the latest window
        "2026-11-08": 5,   # last day of the previous window
        "2026-11-02": 3,   # first day of the previous window
        "2026-11-01": 100,  # older than the previous window
    }

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for day, outcomes in seeded.items():
            _seed_period(db, "worker", day, outcomes)

    result = await _agents(seed, grain="days")
    assert result["caption"] == "Last 7 completed days"
    assert result["period"]["latest"] == {"from": "2026-11-09", "to": "2026-11-15"}
    assert result["period"]["previous"] == {"from": "2026-11-02", "to": "2026-11-08"}
    outcomes = result["rows"][0]["metrics"]["outcomes"]
    assert [point["period"] for point in outcomes["periods"]] == [f"2026-11-{day:02d}" for day in range(9, 16)]
    assert outcomes["n"] == 20 and outcomes["value"] == pytest.approx(20 / 7)
    assert outcomes["previous_n"] == 8 and outcomes["previous_value"] == pytest.approx(8 / 7)
    assert result["rows"][0]["n"] == 20
    assert [item["outcomes"] for item in result["project_summaries"]] == [20]


@pytest.mark.asyncio
@pytest.mark.parametrize("now,offset_hours,monday,previous_monday", (
    # US fall-back (Sunday 2026-11-01): the period of 2026-11-02 starts in PST, 08:00Z.
    (datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc), 8, "2026-11-02", "2026-10-26"),
    # US spring-forward (Sunday 2026-03-08): the period of 2026-03-09 starts in PDT, 07:00Z.
    (datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc), 7, "2026-03-09", "2026-03-02"),
), ids=("fall-back", "spring-forward"))
async def test_a_period_starts_at_local_midnight_on_either_side_of_a_dst_change(
    isolated_db, monkeypatch, now, offset_hours, monday, previous_monday
):
    """A Monday at local midnight is 07:00Z in PDT and 08:00Z in PST, and the week follows it.

    The task one second before the real boundary belongs to the previous period and the task at it
    to the new Monday; the 07:00Z task on the PST side proves a PDT offset was not reused.
    """
    from datetime import timedelta

    boundary = datetime.fromisoformat(f"{monday}T00:00:00+00:00") + timedelta(hours=offset_hours)
    before, after = boundary - timedelta(seconds=1), boundary

    def at(instant):
        return instant.strftime("%Y-%m-%dT%H:%M:%SZ")

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        _seed_period(db, "worker", "before", 1, at=at(before))
        _seed_period(db, "worker", "after", 1, at=at(after))
        if offset_hours == 8:
            # 07:00Z is Sunday 23:00 local in PST: still the previous period.
            _seed_period(db, "worker", "pdt-offset", 1, at=at(boundary - timedelta(hours=1)))

    result = _build(seed, now=now, tz="America/Los_Angeles", measured=_always_measured)
    counts = _outcome_counts(result["rows"][0])
    assert counts[monday] == 1, counts
    assert counts[previous_monday] == (2 if offset_hours == 8 else 1), counts


@pytest.mark.asyncio
async def test_a_half_hour_zone_buckets_days_at_its_own_local_midnight(isolated_db):
    """Asia/Kolkata is +05:30, so a local day starts at 18:30Z: its last second is 18:29:59Z."""
    now = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        _seed_period(db, "worker", "last-second", 1, at="2026-11-12T18:29:59Z")
        _seed_period(db, "worker", "first-second", 1, at="2026-11-12T18:30:00Z")

    result = _build(seed, now=now, kind="days", tz="Asia/Kolkata", measured=_always_measured)
    counts = _outcome_counts(result["rows"][0])
    assert counts["2026-11-12"] == 1, counts
    assert counts["2026-11-13"] == 1, counts
    assert sum(counts.values()) == 2, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("grain", ["days", "weeks"])
async def test_all_work_serves_a_populated_agents_table_at_both_grains(isolated_db, monkeypatch, grain):
    """All work reads the prepared per-agent grains of the page's own grain; a missing grain was a 500."""
    _freeze(monkeypatch, POST_BREAK)

    def seed(db):
        _seed_project(db, "p1")
        _seed_agent(db, "worker", project_id="p1")
        for period in _frame(9)[-4:]:
            _seed_period(db, "worker", period, 25)

    body = await _agents(seed, grain=grain)
    assert body["period"]["grain"] == grain
    assert [row["agent"]["id"] for row in body["rows"]] == ["worker"]
    assert "dollars_per_work_hour" in body["rows"][0]["metrics"]


def _seed_minutes_agent(now: datetime):
    """One agent with 25 outcomes and one human message in each period of both windows."""
    from api.measurement_scope import resolve_grain

    grain = resolve_grain("weeks", "UTC", now=now)

    def seed(db):
        _seed_agent(db, "worker")
        for period in (*grain.previous.periods, *grain.latest.periods):
            _seed_period(db, "worker", period, 25)
            db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
                       "VALUES (?,?,'user','x','{}',?)", (f"m-{period}", "worker", f"{period}T12:00:00Z"))
    return seed


def test_a_previous_window_minutes_recording_did_not_cover_has_no_figure(isolated_db):
    """Minutes are recorded from 2026-08-31: a previous window starting before it reads "new".

    Pooling only its recorded weeks would put a four-week figure beside a one-week figure and
    report the difference in coverage as a change in the work.
    """
    cell = _build(_seed_minutes_agent(datetime(2026, 10, 10, 9, tzinfo=timezone.utc)),
                  now=datetime(2026, 10, 10, 9, tzinfo=timezone.utc))["rows"][0]["metrics"]["active_hours_per_outcome"]
    assert cell["value"] is not None and cell["previous_value"] is None


def test_a_previous_window_minutes_recording_covered_keeps_its_figure(isolated_db):
    now = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)
    cell = _build(_seed_minutes_agent(now), now=now)["rows"][0]["metrics"]["active_hours_per_outcome"]
    assert cell["value"] is not None and cell["previous_value"] == pytest.approx(cell["value"])
