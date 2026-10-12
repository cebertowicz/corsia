"""Tests for the read-cost speed gate: the ratchet rule, counting, route targets and the CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speed_gate import cli, fixture_build, fixture_excluded, fixture_rows, harness, ratchet, report, routes, runner  # noqa: E402
from speed_gate.sqlite_pin import PINNED_SQLITE, PinError, sqlite3  # noqa: E402  (the pinned build, not the stdlib one)
from speed_gate import sqlite_pin  # noqa: E402

VERSION = PINNED_SQLITE


def cost(steps=2000, statements=5, size=500, status=200, **extra):
    return {"steps": steps, "statements": statements, "bytes": size, "status": status, **extra}


def result(routes_, version=VERSION, **extra):
    return {"sqlite_version": version, "progress_every": 100, "routes": routes_, "not_called": {}, **extra}


def baseline_for(routes_, version=VERSION):
    return ratchet.new_baseline(result(routes_, version), "test baseline")


def kinds(verdict):
    return sorted((item.route, item.kind, item.metric) for item in verdict.findings)


def test_unchanged_costs_pass():
    base = baseline_for({"/a": cost(), "/b": cost(900_000)})
    assert ratchet.compare(result({"/a": cost(), "/b": cost(900_000)}), base).status == "pass"


def test_growth_over_a_quarter_fails_and_a_quarter_passes():
    base = baseline_for({"/a": cost(100_000)})
    assert ratchet.compare(result({"/a": cost(125_000)}), base).status == "pass"
    verdict = ratchet.compare(result({"/a": cost(126_000)}), base)
    assert verdict.status == "would_fail"
    assert kinds(verdict) == [("/a", "growth", "steps")]


def test_small_absolute_rise_on_a_tiny_route_is_not_growth():
    base = baseline_for({"/a": cost(2_000)})
    assert ratchet.compare(result({"/a": cost(3_000)}), base).status == "pass"


def test_statement_growth_is_its_own_signal():
    base = baseline_for({"/a": cost(statements=40)})
    verdict = ratchet.compare(result({"/a": cost(statements=60)}), base)
    assert kinds(verdict) == [("/a", "growth", "statements")]


def test_new_route_over_a_ceiling_fails_and_under_passes():
    base = baseline_for({"/a": cost()})
    over = ratchet.compare(result({"/a": cost(), "/new": cost(500_001)}), base)
    assert kinds(over) == [("/new", "ceiling", "steps")]
    under = ratchet.compare(result({"/a": cost(), "/new": cost(499_000)}), base)
    assert under.status == "pass"
    assert kinds(under) == [("/new", "new", "steps")]


def test_new_route_over_statement_ceiling_fails():
    verdict = ratchet.compare(result({"/new": cost(statements=501)}), baseline_for({}))
    assert kinds(verdict) == [("/new", "ceiling", "statements")]


def test_grandfathered_route_is_judged_by_growth_not_the_ceiling():
    base = baseline_for({"/slow": cost(3_000_000)})
    assert base["grandfathered"] == ["/slow"]
    assert ratchet.compare(result({"/slow": cost(3_000_000)}), base).status == "pass"
    assert ratchet.compare(result({"/slow": cost(4_000_000)}), base).status == "would_fail"


def test_a_route_that_crosses_the_ceiling_without_being_grandfathered_fails():
    base = baseline_for({"/a": cost(400_000)})
    verdict = ratchet.compare(result({"/a": cost(510_000)}), base)
    assert ("/a", "ceiling", "steps") in kinds(verdict)


def test_improvement_is_reported_but_never_fails():
    base = baseline_for({"/a": cost(1_000_000)})
    verdict = ratchet.compare(result({"/a": cost(100_000)}), base)
    assert verdict.status == "pass"
    assert kinds(verdict) == [("/a", "improved", "steps")]


def test_version_mismatch_is_neither_pass_nor_compared():
    base = baseline_for({"/a": cost()}, version="3.49.0")
    verdict = ratchet.compare(result({"/a": cost(9_999_999)}), base)
    assert verdict.status == "version_mismatch"
    assert "REBASELINE NEEDED" in verdict.message
    assert verdict.findings == []


def test_error_responses_are_not_measured():
    current = result({"/a": cost(), "/broken": cost(9_999_999, status=500)})
    assert set(ratchet.measured(current)) == {"/a"}
    assert "/broken" not in ratchet.new_baseline(current, "x")["routes"]


def test_report_names_the_would_fail_route_and_the_mode():
    base = baseline_for({"/a": cost(100_000)})
    current = result({"/a": cost(300_000)})
    text = report.render(current, ratchet.compare(current, base), base, report_only=True, base_label="the baseline")
    assert "would fail" in text and "`/a`" in text and "report-only" in text


def test_counting_records_steps_and_statements_and_ignores_time(monkeypatch):
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    counters = harness.Counters()
    harness.install_counting(counters)
    try:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t(x)")
        db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(20_000)])
        before = counters.snapshot()
        db.execute("SELECT COUNT(*) FROM t WHERE x % 7 = 0").fetchone()
        steps, statements = (after - prior for after, prior in zip(counters.snapshot(), before))
        assert statements == 1 and steps >= 20_000
        again = counters.snapshot()
        db.execute("SELECT COUNT(*) FROM t WHERE x % 7 = 0").fetchone()
        assert abs((counters.snapshot()[0] - again[0]) - steps) <= 2 * harness.PROGRESS_EVERY
    finally:
        sqlite3.connect = real_connect


def test_route_target_fills_path_and_required_query_and_reports_the_unfillable():
    route = SimpleNamespace(dependant=SimpleNamespace(query_params=[]))
    ok = routes.build_target("/api/threads/{thread_id}/x", route, {"thread_id": "t 1"}, {}, "tok")
    assert ok.url.startswith("/api/threads/t%201/x")
    missing = routes.build_target("/api/things/{thing_id}", route, {}, {}, "tok")
    assert missing.url is None and "thing_id" in missing.reason
    static = routes.build_target("/docs", route, {}, {}, "tok")
    assert static.url is None and "no application read" in static.reason


def test_update_baseline_needs_a_reason_and_grandfathers_over_ceiling_routes(tmp_path):
    current = tmp_path / "current.json"
    current.write_text(json.dumps(result({"/a": cost(), "/slow": cost(900_000)})))
    target = tmp_path / "baseline.json"
    assert cli.main(["update-baseline", "--current", str(current), "--baseline", str(target), "--reason", " "]) == 2
    assert not target.exists()
    assert cli.main(["update-baseline", "--current", str(current), "--baseline", str(target),
                     "--reason", "initial"]) == 0
    written = json.loads(target.read_text())
    assert written["reason"] == "initial" and written["grandfathered"] == ["/slow"]


@pytest.mark.parametrize("enforce, expected", [(False, 0), (True, 1)])
def test_compare_is_report_only_unless_enforced(tmp_path, enforce, expected):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(baseline_for({"/a": cost(100_000)})))
    current = tmp_path / "current.json"
    current.write_text(json.dumps(result({"/a": cost(400_000)})))
    args = ["compare", "--current", str(current), "--baseline", str(base)] + (["--enforce"] if enforce else [])
    assert cli.main(args) == expected


@pytest.mark.parametrize("enforce, expected", [(False, 0), (True, cli.EXIT_REBASELINE)])
def test_version_mismatch_warns_in_report_only_and_exits_two_when_enforcing(tmp_path, capsys, enforce, expected):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(baseline_for({"/a": cost()}, version="3.49.0")))
    current = tmp_path / "current.json"
    current.write_text(json.dumps(result({"/a": cost()})))
    summary = tmp_path / "summary.md"
    args = ["compare", "--current", str(current), "--baseline", str(base), "--summary", str(summary)]
    assert cli.main(args + (["--enforce"] if enforce else [])) == expected
    assert "::warning" in capsys.readouterr().out
    assert "REBASELINE NEEDED" in summary.read_text()


def test_a_small_route_moving_by_measured_noise_is_not_growth():
    # The floor keeps a route of a few thousand steps from being flagged for a couple of hundred more.
    base = baseline_for({"/a": cost(2_400)})
    assert ratchet.compare(result({"/a": cost(2_700)}), base).status == "pass"
    # A regression of interest (per-project usage rollups read on every /api/projects call added ~720k steps)
    # is far above the floor.
    assert ratchet.compare(result({"/a": cost(2_400 + 724_000)}), base).status == "would_fail"


def test_measurement_runs_with_an_empty_home_and_account_columns_reference_real_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/somewhere/on/the/host")
    env = runner._env(tmp_path)
    assert env["HOME"] == str(tmp_path / "home") and "CODEX_HOME" not in env
    pools = SimpleNamespace(ids={"vendor_account": ["a1"]})
    assert [fixture_rows.pool_for(column, pools) for column in ("account_id", "pending_account_id", "vendor_account_id")] \
        == ["vendor_account"] * 3


def test_a_stuck_stage_is_stopped_and_reports_where_it_stopped(tmp_path, capsys, monkeypatch):
    stuck = tmp_path / "stuck.py"
    stuck.write_text("import time\nprint('route 7: /api/slow', flush=True)\ntime.sleep(60)\n")
    monkeypatch.setattr(runner, "_env", lambda root: {**__import__("os").environ, "PYTHONPATH": str(tmp_path)})
    with pytest.raises(runner.MeasurementError, match=r"(?s)stopped after 1s.*route 7: /api/slow"):
        runner._run("stuck", [], tmp_path, timeout=1)
    assert "route 7: /api/slow" in capsys.readouterr().err


def test_a_difference_between_runs_names_the_statement_and_thread_that_differ():
    def run(trace):
        return {"routes": {"/a": {"info": {"trace": trace}}}}
    common = ["portal: SELECT 1", "db_0: SELECT 2"]
    lines = cli._trace_difference([run(common + ["db_0: SELECT 3"]), run(common)], "/a")
    assert lines == ["    only in run 1: db_0: SELECT 3 (x1)"]


MANIFEST = {"size_bytes": 1, "sqlite_version": VERSION, "builder_sha256": "x", "schema": "s", "excluded": {}, "short": {}}


def disagreements(first, second):
    return sorted((item.route, item.metric, item.tolerated) for item in ratchet.compare_runs(first, second))


def test_runs_of_one_tree_must_agree_exactly_on_steps_and_statements():
    first = result({"/a": cost(100_000)})
    assert disagreements(first, result({"/a": cost(100_000)})) == []
    assert disagreements(first, result({"/a": cost(100_001)})) == [("/a", "steps", False)]
    assert disagreements(first, result({"/a": cost(100_000, statements=6)})) == [("/a", "statements", False)]
    assert disagreements(first, result({"/b": cost()})) == [("/a", "measured", False), ("/b", "measured", False)]


def test_bytes_are_exact_except_on_a_route_with_a_committed_tolerance_and_reason():
    live = "/api/system"
    assert live in ratchet.BYTE_TOLERANCE and ratchet.BYTE_TOLERANCE[live][1]
    tolerance = ratchet.BYTE_TOLERANCE[live][0]
    first = result({live: cost(size=1000), "/api/other": cost(size=1000)})
    within = result({live: cost(size=1000 + tolerance), "/api/other": cost(size=1000)})
    assert disagreements(first, within) == [(live, "bytes", True)]
    beyond = result({live: cost(size=1000 + tolerance + 1), "/api/other": cost(size=1001)})
    assert disagreements(first, beyond) == [("/api/other", "bytes", False), (live, "bytes", False)]
    # the tolerance never reaches steps or statements
    assert disagreements(first, result({live: cost(steps=2001, size=1000), "/api/other": cost(size=1000)})) \
        == [(live, "steps", False)]


def test_a_baselined_route_that_errors_or_cannot_be_called_is_an_error_not_a_removal():
    base = baseline_for({"/a": cost(), "/b": cost(), "/c": cost(), "/gone": cost()})
    current = result({"/a": cost(), "/b": cost(9, status=500)}, not_called={"/c": "needs a path parameter"})
    verdict = ratchet.compare(current, base)
    assert verdict.status == "would_fail"
    assert kinds(verdict) == [("/b", "error", "status"), ("/c", "error", "status"), ("/gone", "removed", "steps")]
    assert [item.route for item in verdict.failing] == ["/b", "/c"]
    assert ratchet.compare(result({"/a": cost(), "/b": cost(), "/c": cost()}), base).status == "pass"


def test_report_lists_removed_routes_drained_background_work_and_excluded_tables():
    base = baseline_for({"/a": cost(), "/gone": cost()})
    current = result({"/a": cost(info={"background_tasks": ["api/usage_rollup.py:refresh"]})},
                     fixture_manifest={"excluded": {"llm_usage_hourly": "derived from llm_usage"}})
    text = report.render(current, ratchet.compare(current, base), base, report_only=True, base_label="the baseline")
    assert "`/gone`" in text and "api/usage_rollup.py:refresh" in text and "llm_usage_hourly" in text
    assert "derived from llm_usage" in text


def test_report_only_prints_a_disagreement_and_stays_green_enforcing_exits_3(tmp_path, monkeypatch, capsys):
    runs = [result({"/a": cost(100_000), "/b": cost()}), result({"/a": cost(100_001), "/b": cost()})]
    monkeypatch.setattr(cli.runner, "build_and_measure", lambda backend, repeats: (runs, MANIFEST, {}))
    out, summary = tmp_path / "m.json", tmp_path / "summary.md"
    _, code = cli.measure_tree(tmp_path, 2, keep_runs=out, summary=summary)
    captured = capsys.readouterr()
    assert code == 0 and "::warning title=Read-cost speed gate" in captured.out and "/b" not in captured.err
    assert "run 1: steps 100,000, statements 5" in captured.err and "run 2: steps 100,001, statements 5" in captured.err
    assert "NOT tolerated" in captured.err
    assert "report-only: the job stays green" in summary.read_text() and "`/a`" in summary.read_text()
    assert json.loads(Path(f"{out}.run2.json").read_text())["routes"]["/a"]["steps"] == 100_001
    _, code = cli.measure_tree(tmp_path, 2, enforce=True)
    assert code == cli.EXIT_NONDETERMINISTIC


def test_a_tolerated_byte_difference_is_listed_and_never_fails(tmp_path, monkeypatch, capsys):
    runs = [result({"/api/system": cost(size=1000)}), result({"/api/system": cost(size=1004)})]
    monkeypatch.setattr(cli.runner, "build_and_measure", lambda backend, repeats: (runs, MANIFEST, {}))
    for enforce in (False, True):
        _, code = cli.measure_tree(tmp_path, 2, enforce=enforce, summary=tmp_path / "summary.md")
        assert code == 0
    printed = capsys.readouterr().err
    assert "bytes run 1 1,000, run 2 1,004, difference 4: within its tolerance of 8" in printed
    assert "NONDETERMINISTIC" not in printed and not (tmp_path / "summary.md").exists()


def test_enforcing_also_fails_when_a_route_improved_so_the_baseline_cannot_go_stale(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(baseline_for({"/a": cost(1_000_000)})))
    current = tmp_path / "current.json"
    current.write_text(json.dumps(result({"/a": cost(100_000)})))
    args = ["compare", "--current", str(current), "--baseline", str(base)]
    assert cli.main(args) == 0
    assert cli.main(args + ["--enforce"]) == cli.EXIT_WOULD_FAIL
    assert "update-baseline" in capsys.readouterr().err


def test_report_only_compare_names_the_routes_that_would_fail_or_should_drop_and_exits_zero(tmp_path, capsys):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(baseline_for({"/grew": cost(100_000), "/fell": cost(1_000_000), "/gone": cost()})))
    current = tmp_path / "current.json"
    current.write_text(json.dumps(result({"/grew": cost(400_000), "/fell": cost(100_000)})))
    assert cli.main(["compare", "--current", str(current), "--baseline", str(base)]) == 0
    out = capsys.readouterr().out
    assert "speed-gate: would fail on /grew\n" in out
    assert "speed-gate: baseline should drop on /fell, /gone\n" in out


@pytest.fixture
def background(tmp_path, monkeypatch):
    """A ``Background`` rooted at ``tmp_path``; the global create_task patch is undone after the test."""
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_task", asyncio.BaseEventLoop.create_task)
    namespace: dict = {}
    source = (
        "import asyncio\n"
        "async def refresh():\n    await asyncio.sleep(0.01)\n"
        "async def explode():\n    raise ValueError('boom')\n"
        "async def slow():\n    await asyncio.sleep(5)\n"
        "async def request(loop_holder):\n    loop_holder.append(asyncio.get_running_loop().create_task(refresh()))\n"
        "async def spawn_unparented(loop):\n    loop.call_soon(lambda: loop.create_task(refresh()))\n    await asyncio.sleep(0.05)\n"
    )
    exec(compile(source, str(tmp_path / "app.py"), "exec"), namespace)
    return harness.Background(tmp_path), namespace


def test_drain_waits_for_a_task_the_request_started_and_names_it(background):
    bg, app = background

    async def scenario():
        bg.portal = SimpleNamespace(call=lambda fn: None)
        await app["request"]([])
        return await bg._wait()

    assert asyncio.run(scenario()) == ["app.py:refresh"]


def test_a_background_task_with_no_running_parent_is_an_error_no_verdict(background):
    bg, app = background

    async def scenario():
        await app["spawn_unparented"](asyncio.get_running_loop())
        await bg._wait()
        bg.check_traceable()

    with pytest.raises(harness.GuardError, match="not traceable to the measured request: app.py:refresh"):
        asyncio.run(scenario())


def test_a_drained_task_that_raises_or_never_finishes_is_an_error(background, monkeypatch):
    bg, app = background

    async def raises():
        asyncio.get_running_loop().create_task(app["explode"]())
        await asyncio.sleep(0.01)
        await bg._wait()

    with pytest.raises(harness.GuardError, match="app.py:explode raised"):
        asyncio.run(raises())
    monkeypatch.setattr(harness, "DRAIN_TIMEOUT_SECONDS", 0.05)

    async def hangs():
        asyncio.get_running_loop().create_task(app["slow"]())
        await bg._wait()

    with pytest.raises(harness.GuardError, match="did not finish in 0.05s: app.py:slow"):
        asyncio.run(hangs())


def test_a_window_opening_with_an_application_task_alive_is_an_error(background):
    bg, app = background

    async def scenario():
        task = asyncio.get_running_loop().create_task(app["slow"]())
        try:
            bg.assert_quiet()
        finally:
            task.cancel()

    with pytest.raises(harness.GuardError, match="alive when a measured window opened: app.py:slow"):
        asyncio.run(scenario())


def test_the_one_worker_per_lane_proof_fails_on_a_second_worker_and_on_no_lane_at_all():
    counters = harness.Counters()
    counters.threads = {"db_0", "db-read_0", "asyncio-portal-x"}
    harness.assert_one_worker_per_lane(counters)
    counters.threads = {"db_0", "db_1"}
    with pytest.raises(harness.GuardError, match="more than one worker thread"):
        harness.assert_one_worker_per_lane(counters)
    counters.threads = {"asyncio-portal-x"}
    with pytest.raises(harness.GuardError, match="no database lane thread ran"):
        harness.assert_one_worker_per_lane(counters)


HEAVY = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<20000) SELECT count(*) FROM c"


@pytest.fixture
def counted(monkeypatch):
    """``(counters, connect)``: connections made through the harness's own ``sqlite3.connect`` wrapper."""
    counters = harness.Counters()
    monkeypatch.setattr(sqlite3, "connect", sqlite3.connect)  # undone after the test
    monkeypatch.setattr(harness, "COUNTING_CLASSES", {}, raising=False)
    harness.install_counting(counters)
    return counters, lambda: sqlite3.connect(":memory:")


def run_heavy(counters, db):
    before = counters.steps
    db.execute(HEAVY).fetchone()
    return counters.steps - before


def test_an_application_progress_handler_neither_replaces_nor_clears_the_count(counted):
    counters, connect = counted
    db = connect()
    baseline = run_heavy(counters, db)
    assert baseline > 100_000
    calls: list[int] = []
    db.set_progress_handler(lambda: calls.append(1) or 0, 1_000)  # a deadline check, as the audit and log reads set
    assert run_heavy(counters, db) == baseline
    assert len(calls) == baseline // 1_000
    db.set_progress_handler(None, 0)  # the lesson report clears its handler when it is done
    seen = len(calls)
    assert run_heavy(counters, db) == baseline
    assert len(calls) == seen


def test_an_application_handler_still_aborts_a_statement_through_the_counter(counted):
    counters, connect = counted
    db = connect()
    db.set_progress_handler(lambda: True, 1_000)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        db.execute(HEAVY).fetchone()
    assert counters.steps > 0


def test_an_application_trace_callback_is_chained_and_clearing_it_keeps_the_statement_count(counted):
    counters, connect = counted
    db = connect()
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    db.execute("SELECT 1")
    assert counters.statements == 1 and seen == ["SELECT 1"]
    db.set_trace_callback(None)
    db.execute("SELECT 2")
    assert counters.statements == 2 and seen == ["SELECT 1"]


def test_a_live_connection_the_counter_does_not_own_is_an_error_no_verdict(counted, monkeypatch):
    counters, connect = counted
    owned = connect()
    foreign = sqlite3.Connection(":memory:")  # not made by sqlite3.connect, so nothing counts it
    closed = sqlite3.Connection(":memory:")
    closed.close()
    monkeypatch.setattr(harness.gc, "get_objects", lambda: [owned, closed])
    harness.assert_all_counted()
    monkeypatch.setattr(harness.gc, "get_objects", lambda: [owned, closed, foreign])
    with pytest.raises(harness.GuardError, match=r"1 live SQLite connection\(s\) are not counted"):
        harness.assert_all_counted()


def test_the_harness_refuses_a_sqlite_that_is_not_the_pinned_build(monkeypatch):
    fake = SimpleNamespace(sqlite_version="3.45.1", dbapi2=object())
    monkeypatch.setitem(sys.modules, "sqlean", fake)
    monkeypatch.delitem(sys.modules, "sqlite3")
    with pytest.raises(PinError, match="3.45.1 is loaded but the gate pins " + PINNED_SQLITE):
        sqlite_pin.install()


def test_the_pin_is_a_dev_only_requirement_with_exact_version_and_hashes():
    pinned = (Path(__file__).resolve().parents[1] / "speed_gate" / "requirements.txt").read_text()
    assert sqlite_pin.PINNED_PACKAGE in pinned and pinned.count("--hash=sha256:") >= 1
    runtime = (Path(__file__).resolve().parents[2] / "backend" / "requirements.txt").read_text()
    assert "sqlean" not in runtime


def test_the_build_fails_on_an_empty_table_with_no_listed_reason_and_records_the_listed_ones():
    listed = sorted(fixture_excluded.EXCLUDED)[:1]
    manifest = {"no_target": [*listed], "unseeded": []}
    fixture_build._account_for_empty_tables(manifest)
    assert manifest["excluded"] == {listed[0]: fixture_excluded.EXCLUDED[listed[0]]}
    manifest = {"no_target": ["brand_new_table"], "unseeded": [{"table": "other_new", "reason": "IntegrityError: x"}]}
    with pytest.raises(fixture_build.FixtureCoverageError, match="brand_new_table .*other_new \\(IntegrityError: x\\)"):
        fixture_build._account_for_empty_tables(manifest)
    assert all(reason.strip() for reason in fixture_excluded.EXCLUDED.values())


def row_target_db(monkeypatch, counts, targets, short):
    db = sqlite3.connect(":memory:")
    for table, count in counts.items():
        db.execute(f"CREATE TABLE {table} (id INTEGER)")
        db.executemany(f"INSERT INTO {table} VALUES (?)", [(i,) for i in range(count)])
    monkeypatch.setattr(fixture_build.spec, "target_rows", targets.get)
    monkeypatch.setattr(fixture_excluded, "SHORT", short)
    return db


def test_a_table_below_its_census_target_fails_the_build_unless_listed_with_its_exact_count(monkeypatch):
    targets = {"full": 3, "wide": 3, "short": 5}
    manifest = {"tables": {}}
    db = row_target_db(monkeypatch, {"full": 3, "wide": 9, "short": 2, "untargeted": 0}, targets,
                       {"short": (2, "bounded by the thread count")})
    fixture_build._check_row_targets(db, manifest)
    assert manifest["short"] == {"short": {"target": 5, "rows": 2, "reason": "bounded by the thread count"}}
    for short, message in (({}, r"short has 2 row\(s\), target 5, not listed"),
                           ({"short": (3, "x")}, r"short has 2 row\(s\), target 5, listed as 3"),
                           ({"short": (2, "x"), "full": (3, "x")}, r"full has 3 row\(s\), at its target of 3"),
                           ({"short": (2, "x"), "ghost": (1, "x")}, r"ghost is listed as short but has no census target")):
        db = row_target_db(monkeypatch, {"full": 3, "wide": 9, "short": 2}, targets, short)
        with pytest.raises(fixture_build.FixtureCoverageError, match=message):
            fixture_build._check_row_targets(db, {"tables": {}})


def test_the_report_lists_fixture_tables_below_their_target_with_the_reason():
    current = result({"/a": cost()}, fixture_manifest={"short": {"wakes": {"target": 966, "rows": 553, "reason": "thread_id is UNIQUE"}}})
    text = report.render(current, ratchet.compare(current, baseline_for({"/a": cost()})), baseline_for({"/a": cost()}),
                         report_only=True, base_label="the baseline")
    assert "`wakes` | 966 | 553 | thread_id is UNIQUE" in text
