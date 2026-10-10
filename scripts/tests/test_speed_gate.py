"""Tests for the read-cost speed gate: the ratchet rule, counting, route targets and the CLI."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speed_gate import cli, fixture_rows, harness, ratchet, report, routes, runner  # noqa: E402

VERSION = "3.50.4"


def cost(steps=2000, statements=5, size=500, status=200):
    return {"steps": steps, "statements": statements, "bytes": size, "status": status}


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


def unstable(first, second):
    return sorted((item.route, item.metric) for item in ratchet.compare_runs(first, second) if not item.within_band)


def test_run_to_run_steps_may_differ_by_one_percent_of_the_larger_run_but_statements_must_match():
    first = result({"/a": cost(100_000, size=1000)})
    # 1% of the larger run (1,006) is the band on this route; the 600-step floor is below it
    assert unstable(first, result({"/a": cost(100_900, size=1005)})) == []
    assert unstable(first, result({"/a": cost(101_200, size=1000)})) == [("/a", "steps")]
    assert unstable(first, result({"/a": cost(100_000, statements=6, size=1000)})) == [("/a", "statements")]
    assert unstable(first, result({"/b": cost()})) == [("/a", "measured"), ("/b", "measured")]


def test_the_step_band_grows_with_the_route_and_has_a_floor_for_small_ones():
    assert ratchet.run_band("steps", 11_238_100, 11_238_800) == 112_388
    assert ratchet.run_band("steps", 1_890_000, 1_892_500) == 18_925
    assert ratchet.run_band("steps", 20_000, 20_500) == ratchet.STEP_SLACK
    assert ratchet.run_band("statements", 51, 51) == 0


def test_a_difference_inside_the_band_is_still_listed_with_both_numbers_and_its_band():
    items = ratchet.compare_runs(result({"/a": cost(1_892_500)}), result({"/a": cost(1_890_000)}))
    assert [(item.metric, item.first, item.second, item.band, item.within_band) for item in items] \
        == [("steps", 1_892_500, 1_890_000, 18_925, True)]


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
    # /api/vendor-accounts was seen at 2400 and 2700 steps across runs of one tree.
    base = baseline_for({"/a": cost(2_400)})
    assert ratchet.compare(result({"/a": cost(2_700)}), base).status == "pass"
    # A regression of interest (pre-691 /api/projects added 724k steps) is far above the floor.
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


MANIFEST = {"size_bytes": 1, "sqlite_version": VERSION, "builder_sha256": "x", "schema": "s"}


def test_runs_that_disagree_beyond_the_band_are_a_warning_report_only_and_exit_3_when_enforcing(tmp_path, monkeypatch, capsys):
    runs = [result({"/a": cost(100_000), "/b": cost()}), result({"/a": cost(102_000, statements=6), "/b": cost()})]
    monkeypatch.setattr(cli.runner, "build_and_measure", lambda backend, repeats: (runs, MANIFEST, {}))
    out, summary = tmp_path / "m.json", tmp_path / "summary.md"
    _, code = cli.measure_tree(tmp_path, 2, keep_runs=out, summary=summary)
    captured = capsys.readouterr()
    assert code == 0 and "::warning title=Read-cost speed gate" in captured.out and "/b" not in captured.err
    assert "run 1: steps 100,000, statements 5" in captured.err and "run 2: steps 102,000, statements 6" in captured.err
    assert "statements run 1 5, run 2 6, difference 1, band 0: OUTSIDE the band" in captured.err
    assert "report-only: the job stays green" in summary.read_text() and "`/a`" in summary.read_text()
    assert json.loads(Path(f"{out}.run2.json").read_text())["routes"]["/a"]["steps"] == 102_000
    _, code = cli.measure_tree(tmp_path, 2, enforce=True)
    assert code == cli.EXIT_NONDETERMINISTIC


def test_runs_that_differ_inside_the_band_print_the_numbers_and_never_fail(tmp_path, monkeypatch, capsys):
    runs = [result({"/a": cost(1_892_500)}), result({"/a": cost(1_890_000)})]
    monkeypatch.setattr(cli.runner, "build_and_measure", lambda backend, repeats: (runs, MANIFEST, {}))
    for enforce in (False, True):
        _, code = cli.measure_tree(tmp_path, 2, enforce=enforce, summary=tmp_path / "summary.md")
        assert code == 0
    printed = capsys.readouterr().err
    assert "steps run 1 1,892,500, run 2 1,890,000, difference 2,500, band 18,925: within the band" in printed
    assert "NONDETERMINISTIC" not in printed and not (tmp_path / "summary.md").exists()


def test_a_difference_between_runs_names_the_statement_and_thread_that_differ():
    def run(trace):
        return {"routes": {"/a": {"info": {"trace": trace}}}}
    common = ["portal: SELECT 1", "db_0: SELECT 2"]
    lines = cli._trace_difference([run(common + ["db_0: SELECT 3"]), run(common)], "/a")
    assert lines == ["    only in run 1: db_0: SELECT 3 (x1)"]


def test_database_jobs_are_limited_to_one_worker_so_a_call_never_depends_on_which_thread_ran_it(monkeypatch):
    import importlib.machinery
    import types

    module = types.ModuleType("infra.db_async")
    module.__spec__ = importlib.machinery.ModuleSpec("infra.db_async", None)
    module._pool = SimpleNamespace(_max_workers=8)
    module._read_pool = SimpleNamespace(_max_workers=6)
    module._bulk_pool = SimpleNamespace(_max_workers=2)
    monkeypatch.setitem(sys.modules, "infra", types.ModuleType("infra"))
    monkeypatch.setitem(sys.modules, "infra.db_async", module)
    monkeypatch.setattr(sys.modules["infra"], "db_async", module, raising=False)
    harness._single_db_worker()
    assert [pool._max_workers for pool in (module._pool, module._read_pool, module._bulk_pool)] == [1, 1, 1]


def test_the_usage_rollup_is_built_to_its_end_and_no_request_schedules_a_refresh(monkeypatch):
    import importlib.machinery
    import types

    steps = iter([True, True, False])
    refreshed = []
    rollup = types.ModuleType("api.usage_rollup")
    rollup.__spec__ = importlib.machinery.ModuleSpec("api.usage_rollup", None)
    rollup.refresh_step = lambda db: refreshed.append(db) or next(steps)
    rollup.schedule_refresh = lambda: "scheduled"
    database = types.ModuleType("infra.database")
    database.get_db = lambda: SimpleNamespace(close=lambda: refreshed.append("closed"))
    api = types.ModuleType("api")
    api.usage_rollup = rollup
    for name, module in (("api", api), ("api.usage_rollup", rollup), ("infra", types.ModuleType("infra")),
                         ("infra.database", database)):
        monkeypatch.setitem(sys.modules, name, module)
    harness._settle_usage_rollup()
    assert len(refreshed) == 4 and refreshed[-1] == "closed"  # three steps, then the connection is closed
    assert rollup.schedule_refresh() is None
