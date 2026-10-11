"""The hourly usage rollup serves exactly what the raw ledger would, whatever its state."""
from __future__ import annotations

import asyncio
import functools
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


NOW = datetime(2026, 10, 8, 12, 30)
ZONES = ("UTC", "America/Los_Angeles", "Asia/Kolkata")  # whole-hour, whole-hour, half-hour offset


def _params(zone: str = "UTC") -> dict:
    """The page's request over the seeded weeks, in one viewer zone."""
    from datetime import timezone

    from core.measurement_scope import resolve_grain

    grain = resolve_grain("weeks", zone, now=datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
    return {"scope_project": "*", "scope_agent": None, "user_id": "u", "grain": grain}
MODELS = ("claude-code/claude-sonnet-5-5", "codex/gpt-6.1-sol", "openai/gpt-6.1", "external@srv/deepseek-v4.1-flash")
SOURCES = ("external_harness:claude-code", "external_harness:codex", "conversation", "checkpoint")


def _maybe(rng, value):
    return None if rng.random() < 0.1 else value


def _seed(db, rng, count: int = 600) -> None:
    from api.measurement_prepared import DDL, PREPARED_OUTCOMES, PREPARED_ROOT_OUTCOMES, PREPARED_SCOPE

    for table in (PREPARED_SCOPE, PREPARED_OUTCOMES, PREPARED_ROOT_OUTCOMES):
        db.execute(DDL[table])
    db.executemany("INSERT INTO measurement_scope_threads VALUES (?,?)", [("a", "a"), ("b", "a"), ("c", "c")])
    db.execute("INSERT INTO measurement_outcome_periods VALUES ('2026-09-21', 3, 2, 0)")
    start = datetime(2026, 9, 10)
    for index in range(count):
        created = start + timedelta(minutes=rng.randrange(60 * 24 * 28))
        _insert(db, rng, f"r{index}", created)


def _insert(db, rng, usage_id: str, created: datetime, thread_id: str | None = None) -> None:
    p, c, cw, cr = (_maybe(rng, rng.randrange(0, 300_000)) for _ in range(4))
    total = rng.choice((None, (p or 0) + (c or 0) + (cw or 0) + (cr or 0), 7))
    db.execute(
        "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
        "cache_read_tokens,total_tokens,cost_usd,source,created_at,context_manifest_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (usage_id, thread_id or rng.choice(("a", "b", "c", "elsewhere")), rng.choice(MODELS), p, c, cw, cr, total,
         rng.choice((None, rng.random())), rng.choice(SOURCES), created.isoformat(), None),
    )


def _served(db, zone: str = "UTC") -> dict:
    """Every Measurement usage figure, rounded only where float sums may differ in the last place."""
    from api.measurement_actual import build_actual
    from api.measurement_cost import _usage_rows
    from api.measurement_queries import query_family

    def dollars(value):
        return round(value, 9) if isinstance(value, float) else value

    params = _params(zone)
    actual = build_actual(db, params)
    return {
        "cost": _usage_rows(db, params),
        "actual": {key: dollars(value) for key, value in actual.items() if key != "workspace_subscription"},
        "tokens": query_family(db, "tokens", params),
        "models": query_family(db, "models", params),
        "agents": query_family(db, "tokens", params, grouping="root"),
    }


def _raw(db, zone: str = "UTC") -> dict:
    """What the same reads serve with nothing covered by the rollup."""
    db.execute("SAVEPOINT raw")
    db.execute("UPDATE llm_usage_hourly_state SET built_through=''")
    try:
        return _served(db, zone)
    finally:
        db.execute("ROLLBACK TO raw")
        db.execute("RELEASE raw")


def _build(db, now: datetime = NOW) -> int:
    from api.usage_rollup import refresh_step

    steps = 0
    while refresh_step(db, now):
        steps += 1
        assert steps < 500
    return steps


@pytest.mark.parametrize("zone", ZONES)
def test_a_built_rollup_serves_every_figure_the_raw_ledger_does(isolated_db, zone):
    from infra.database import get_db

    db = get_db()
    _seed(db, random.Random(7))
    before = _served(db, zone)  # nothing built yet: every read is raw
    _build(db)
    assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly").fetchone()[0] > 0
    assert _served(db, zone) == before == _raw(db, zone)


@pytest.mark.parametrize("late_by", [timedelta(hours=1, minutes=50), timedelta(hours=2, minutes=10)])
def test_a_late_insert_just_inside_or_outside_the_grace_window_is_never_lost(isolated_db, late_by):
    from api.usage_rollup import rollup_through
    from infra.database import get_db
    from infra.usage_rollup_schema import GRACE, mark_late_insert

    db = get_db()
    rng = random.Random(11)
    _seed(db, rng)
    now = datetime(2026, 10, 4, 12, 0)  # inside the window, which ends on 2026-10-12
    _build(db, now)
    created = now - late_by
    _insert(db, rng, "late", created)
    mark_late_insert(db, created.isoformat(), now)
    dirty = [row[0] for row in db.execute("SELECT hour FROM llm_usage_hourly_dirty")]
    assert dirty == [created.isoformat()[:13]]  # both are older than the writer's MARK_LEAD limit
    if late_by < GRACE:  # its hour is not covered yet, so it is read raw anyway
        assert dirty[0] >= rollup_through(db)
    else:  # its hour is covered, so it is read raw until rebuilt
        assert dirty[0] < rollup_through(db)
    assert _served(db) == _raw(db)
    _build(db, now)  # the background refresh rebuilds every covered dirty hour and clears it
    covered_dirty = "SELECT COUNT(*) FROM llm_usage_hourly_dirty WHERE hour<?"
    assert db.execute(covered_dirty, (rollup_through(db),)).fetchone()[0] == 0
    assert _served(db) == _raw(db)
    _build(db, now + GRACE)  # once the rollup covers that hour, its mark is rebuilt and cleared
    assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly_dirty").fetchone()[0] == 0
    assert _served(db) == _raw(db)


def test_a_writer_that_commits_after_a_build_read_across_the_hour_tick_is_never_lost(isolated_db):
    """The writer checks its hour just before the grace limit ticks; a build reads after the
    tick but before the writer commits, then moves ``built_through`` past that hour."""
    from api.usage_rollup import rollup_through
    from infra.database import get_db
    from infra.usage_rollup_schema import GRACE, hour_key, mark_late_insert

    db = get_db()
    rng = random.Random(13)
    _seed(db, rng)
    writer_now = datetime(2026, 10, 4, 12, 59, 59)
    _build(db, writer_now)
    created = writer_now - GRACE  # the first hour the rollup may not cover yet
    _build(db, writer_now + timedelta(seconds=2))  # the build's read misses the uncommitted row
    assert rollup_through(db) > hour_key(created.isoformat())
    _insert(db, rng, "late", created, thread_id="a")
    mark_late_insert(db, created.isoformat(), writer_now)  # the writer checked before the tick
    assert _served(db) == _raw(db)


def test_the_rollup_never_covers_an_hour_inside_the_grace_window(isolated_db):
    from api.usage_rollup import rollup_through
    from infra.database import get_db
    from infra.usage_rollup_schema import GRACE, hour_key

    db = get_db()
    _seed(db, random.Random(3), count=50)
    _build(db)
    assert rollup_through(db) == hour_key((NOW - GRACE).isoformat())


def test_refresh_clears_stale_rollup_rows_in_bounded_transactions(isolated_db, monkeypatch):
    from api import usage_rollup
    from infra.database import get_db

    db = get_db()
    _seed(db, random.Random(5))
    _build(db)
    monkeypatch.setattr(usage_rollup, "definition", lambda: "changed")
    assert usage_rollup.refresh_step(db, NOW)  # reset leaves old rows for bounded clearing
    (clear,), more = usage_rollup.plan_step(db, NOW)
    assert more
    assert len(clear.keywords["stale"]) <= usage_rollup.CLEAR_CHUNK_ROWS


def test_refresh_writes_in_chunks_that_each_move_built_through(isolated_db, monkeypatch):
    from api import usage_rollup
    from infra.database import get_db

    db = get_db()
    _seed(db, random.Random(5))
    monkeypatch.setattr(usage_rollup, "CHUNK_ROWS", 3)
    written: list[list[tuple]] = []
    real = usage_rollup._write_range

    def counted(db, expected, low, high, rows):
        written.append(rows)
        return real(db, expected, low, high, rows)

    monkeypatch.setattr(usage_rollup, "_write_range", counted)
    _build(db)
    assert sum(1 for rows in written if rows) > 10
    # At most CHUNK_ROWS per transaction, except one hour that alone has more: an hour is never split.
    assert all(len(rows) <= 3 or len({row[0] for row in rows}) == 1 for rows in written)
    assert _served(db) == _raw(db)


def test_a_migration_that_changes_usage_reads_raw_until_it_is_rebuilt(isolated_db):
    from api.usage_rollup import rollup_through
    from infra.database import get_db
    from infra.migration_92_to_93_host_access_subscription_costs import migrate_92_to_93

    db = get_db()
    _seed(db, random.Random(9))
    row_id = db.execute("SELECT id FROM llm_usage LIMIT 1").fetchone()[0]
    db.execute("UPDATE llm_usage SET model='claude-code/claude-sonnet-5-5', cost_usd=1 WHERE id=?", (row_id,))
    _build(db)
    before = rollup_through(db)
    assert before
    migrate_92_to_93(db)
    expected = _raw(db)
    assert rollup_through(db) == ""
    assert _served(db) == expected
    _build(db)
    assert rollup_through(db) == before
    assert _served(db) == expected


def test_a_plan_only_migration_keeps_the_rollup_covered(isolated_db):
    from api.usage_rollup import rollup_through
    from infra.database import get_db
    from infra.migration_93_to_94_turn_usage_binding_backfill import migrate_93_to_94

    db = get_db()
    _seed(db, random.Random(9))
    bound, unbound = [row[0] for row in db.execute("SELECT id FROM llm_usage LIMIT 2")]
    db.execute(
        """UPDATE llm_usage
           SET thread_id='migration-thread', turn_id='migration-turn',
               plan_id='plan', plan_node_id='node', plan_attempt_id='attempt',
               plan_commitment_id='commitment', plan_usage_role='execution'
           WHERE id=?""",
        (bound,),
    )
    db.execute(
        "UPDATE llm_usage SET thread_id='migration-thread', turn_id='migration-turn' WHERE id=?",
        (unbound,),
    )
    _build(db)
    before = rollup_through(db)
    migrate_93_to_94(db)
    assert db.execute("SELECT plan_id FROM llm_usage WHERE id=?", (unbound,)).fetchone()[0] == "plan"
    assert rollup_through(db) == before
    assert _served(db) == _raw(db)


def test_a_changed_grouping_reads_raw_until_it_is_rebuilt(isolated_db, monkeypatch):
    from api import usage_rollup
    from infra.database import get_db

    db = get_db()
    _seed(db, random.Random(9))
    _build(db)
    expected = _raw(db)
    monkeypatch.setattr(usage_rollup, "definition", lambda: "changed")
    assert usage_rollup.rollup_through(db) == ""
    assert _served(db) == expected
    _build(db)
    assert usage_rollup.rollup_through(db) != ""
    assert _served(db) == expected


def test_a_dirty_hour_planned_before_a_reset_is_not_written_after_it(isolated_db, monkeypatch):
    from api import usage_rollup
    from infra.database import get_db
    from infra.usage_rollup_schema import mark_late_insert

    db = get_db()
    rng = random.Random(17)
    _seed(db, rng)
    _build(db)
    created = datetime(2026, 9, 20, 9, 15)
    _insert(db, rng, "late", created)
    mark_late_insert(db, created.isoformat(), NOW)
    (rebuild,), _more = usage_rollup.plan_step(db, NOW)
    monkeypatch.setattr(usage_rollup, "definition", lambda: "changed")
    assert usage_rollup.refresh_step(db, NOW)  # the reset: nothing is covered any more
    hour_rows = "SELECT COUNT(*) FROM llm_usage_hourly WHERE hour=?"
    before = db.execute(hour_rows, (created.isoformat()[:13],)).fetchone()[0]
    assert rebuild(db) is False
    assert db.execute(hour_rows, (created.isoformat()[:13],)).fetchone()[0] == before
    assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly_dirty").fetchone()[0] == 1
    _build(db)
    assert _served(db) == _raw(db)


@pytest.mark.asyncio
async def test_refresh_is_single_flight_reads_on_one_lane_and_writes_on_the_other():
    from api import usage_rollup

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def read(job):
        calls.append(("read", job.__name__))
        started.set()
        await release.wait()
        return [functools.partial(lambda db: True)], False

    async def write(job, step):
        calls.append(("write", job.__name__))
        finished.set()
        return True

    usage_rollup.schedule_refresh(read, write)
    usage_rollup.schedule_refresh(read, write)  # a second page load while the first refresh runs
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    assert calls == [("read", "_plan_job"), ("write", "_write_job")]


@pytest.mark.asyncio
async def test_the_default_lanes_build_the_rollup(isolated_db):
    """Planning on the bulk lane, which refuses main-database writes, then writing on the pool works."""
    from api import usage_rollup
    from infra.database import get_db

    db = get_db()
    _seed(db, random.Random(13))
    db.commit()
    expected = _raw(db)
    usage_rollup.schedule_refresh()
    task = usage_rollup._refreshing[asyncio.get_running_loop()]
    await asyncio.wait_for(task, 30)
    assert usage_rollup.rollup_through(db) != ""
    assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly").fetchone()[0] > 0
    assert _served(db) == expected


def test_every_measurement_route_starts_the_refresh(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    from api import usage_rollup
    from auth_fixtures import OWNER_TOKEN
    from main import app

    started: list[int] = []
    monkeypatch.setattr(usage_rollup, "schedule_refresh", lambda: started.append(1))
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
    paths = ("agents", "models", "cost", "limits", "key-figures", "where-to-look")
    for path in paths:
        client.get(f"/api/measurement/{path}?project=all&agent=all&grain=days&timezone=UTC", headers=headers)
    assert len(started) == len(paths)


def test_planning_a_step_never_writes(isolated_db):
    """Planning runs on the bulk lane, which refuses main-database writes."""
    from api.usage_rollup import plan_step
    from infra.database import get_db

    db = get_db()
    _seed(db, random.Random(11))
    db.commit()
    while True:
        changes = db.total_changes
        writes, more = plan_step(db, NOW)
        assert db.total_changes == changes and not db.in_transaction
        if not all(write(db) for write in writes) or not more:
            break
    assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly").fetchone()[0] > 0


def test_every_ledger_mutation_is_known_to_the_rollup():
    """A new INSERT, UPDATE or DELETE on llm_usage must keep the rollup exact.

    Inserts go through ``infra.usage_row.write_usage_row``, which marks late hours
    dirty. The one runtime UPDATE binds plan columns, which the rollup does not
    store. Anything else needs invalidation, then an entry here.
    """
    backend = Path(__file__).resolve().parents[1]
    allowed = {
        ("infra/usage_row.py", "INSERT"),
        ("infra/turn_usage_receipts.py", "UPDATE"),  # plan_* columns only; see the rollup's grain
    }
    pattern = re.compile(r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+llm_usage\b(?!_)", re.I)
    found = set()
    for path in backend.rglob("*.py"):
        relative = path.relative_to(backend).as_posix()
        if relative.startswith(("tests/", "infra/migration", ".venv")) or "/migration" in relative:
            continue
        for match in pattern.finditer(path.read_text(errors="ignore")):
            found.add((relative, match.group(1).split()[0].upper()))
    assert found <= allowed, f"teach api.usage_rollup about {sorted(found - allowed)}"


def test_migrations_touching_usage_have_a_reviewed_rollup_disposition():
    """Every migration that names the ledger says whether it affects the hourly grain.

    Any mention counts, not just UPDATE/DELETE text: rebuilds and column helpers
    pass ``"llm_usage"`` as a string, and a backfill INSERT changes old hours too.
    """
    backend = Path(__file__).resolve().parents[1]
    dispositions = {
        # Before schema 100 the rollup did not exist; invalidate_rollup would no-op.
        "migration_7_to_8_plans.py": False,
        "migration_30_to_31_plan_node_cost_indexes.py": False,
        "migration_46_to_47_guidance_manifests.py": False,
        "migration_80_to_81_vendor_accounts.py": False,
        # cost_usd is one of api.usage_rollup.COLUMNS, so the cache becomes stale.
        "migration_92_to_93_host_access_subscription_costs.py": True,
        # plan fields are deliberately absent from the rollup grain.
        "migration_93_to_94_turn_usage_binding_backfill.py": False,
        # reasoning_effort is not an hourly aggregate dimension or value.
        "migration_97_to_98_effort_and_limit_history.py": False,
        # creates the rollup itself; it starts empty and builds from scratch.
        "migration_99_to_100_usage_rollup.py": False,
    }
    pattern = re.compile(r"\bllm_usage\b")
    found = set()
    for path in (backend / "infra").glob("migration_*.py"):
        source = path.read_text()
        if pattern.search(source):
            found.add(path.name)
            assert ("invalidate_rollup(conn)" in source) is dispositions[path.name]
    assert found == set(dispositions), f"review rollup impact for migrations: {sorted(found ^ set(dispositions))}"


def test_the_rollup_grain_holds_no_plan_column():
    from api.usage_rollup import COLUMNS
    from infra.usage_row import PLAN_FIELDS

    assert not set(COLUMNS) & set(PLAN_FIELDS)
