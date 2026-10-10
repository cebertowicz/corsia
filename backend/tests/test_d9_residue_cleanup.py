from __future__ import annotations

import sqlite3
import threading

import pytest


LEGACY_BENCHMARK_DDL = """
CREATE TABLE benchmark_runs (
    id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    approach TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    cost_usd REAL DEFAULT 0.0,
    tokens_used INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0.0,
    output TEXT,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_benchmark_runs_scenario
    ON benchmark_runs(scenario_id, approach);
CREATE INDEX idx_benchmark_runs_status ON benchmark_runs(status);
CREATE TABLE benchmark_results (
    id TEXT PRIMARY KEY,
    run_a_id TEXT NOT NULL REFERENCES benchmark_runs(id),
    run_b_id TEXT NOT NULL REFERENCES benchmark_runs(id),
    judge_model TEXT NOT NULL,
    scores_a TEXT NOT NULL,
    scores_b TEXT NOT NULL,
    reasoning TEXT,
    winner TEXT,
    summary TEXT,
    cost_usd REAL DEFAULT 0.0,
    judge_error TEXT,
    raw_output TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_benchmark_results_runs
    ON benchmark_results(run_a_id, run_b_id);
"""


@pytest.fixture
def init_db_path(tmp_path, monkeypatch):
    from infra import config, database

    db_path = tmp_path / "d9.db"
    database.shutdown_pool()
    database._thread_local = threading.local()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    yield db_path
    database.shutdown_pool()
    database._thread_local = threading.local()


def _schema_names(db_path, kind: str) -> set[str]:
    with sqlite3.connect(db_path) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
            )
        }


def test_model_change_diagnostic_route_is_retired():
    from api.models import router

    assert "/api/model-changes" not in {route.path for route in router.routes}


def test_init_db_fresh_install_omits_unused_benchmark_tables(init_db_path):
    from infra.database import init_db

    init_db()

    assert {"benchmark_runs", "benchmark_results"}.isdisjoint(
        _schema_names(init_db_path, "table")
    )


def test_init_db_refuses_unversioned_database_without_mutating_benchmarks(
    init_db_path,
):
    with sqlite3.connect(init_db_path) as db:
        db.executescript(LEGACY_BENCHMARK_DDL)
        db.executemany(
            """
            INSERT INTO benchmark_runs (
                id, scenario_id, approach, model, status, cost_usd,
                tokens_used, duration_seconds, output, error, started_at,
                completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "run-a", "scenario-1", "direct", "model-a", "complete",
                    1.25, 123, 2.5, "answer-a", None, "start-a", "end-a",
                    "created-a", "updated-a",
                ),
                (
                    "run-b", "scenario-1", "agent", "model-b", "error",
                    2.5, 456, 5.0, None, "boom", "start-b", "end-b",
                    "created-b", "updated-b",
                ),
            ],
        )
        db.execute(
            """
            INSERT INTO benchmark_results (
                id, run_a_id, run_b_id, judge_model, scores_a, scores_b,
                reasoning, winner, summary, cost_usd, judge_error, raw_output,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "result-1", "run-a", "run-b", "judge", '{"score": 1}',
                '{"score": 2}', "reasoning", "b", "summary", 0.5, None,
                "raw", "result-created",
            ),
        )

    from infra.database import SchemaVersionError, init_db

    # An unversioned legacy database has no registered migration reaching it,
    # so startup refuses rather than guessing — leaving its rows untouched.
    with pytest.raises(SchemaVersionError, match="no migration registered"):
        init_db()

    with sqlite3.connect(init_db_path) as db:
        runs = db.execute(
            """
            SELECT id, scenario_id, approach, model, status, cost_usd,
                   tokens_used, duration_seconds, output, error, started_at,
                   completed_at, created_at, updated_at
            FROM benchmark_runs ORDER BY id
            """
        ).fetchall()
        result = db.execute(
            """
            SELECT id, run_a_id, run_b_id, judge_model, scores_a, scores_b,
                   reasoning, winner, summary, cost_usd, judge_error,
                   raw_output, created_at
            FROM benchmark_results
            """
        ).fetchone()
        quick_check = db.execute("PRAGMA quick_check").fetchone()[0]

    assert runs == [
        (
            "run-a", "scenario-1", "direct", "model-a", "complete", 1.25,
            123, 2.5, "answer-a", None, "start-a", "end-a", "created-a",
            "updated-a",
        ),
        (
            "run-b", "scenario-1", "agent", "model-b", "error", 2.5,
            456, 5.0, None, "boom", "start-b", "end-b", "created-b",
            "updated-b",
        ),
    ]
    assert result == (
        "result-1", "run-a", "run-b", "judge", '{"score": 1}',
        '{"score": 2}', "reasoning", "b", "summary", 0.5, None, "raw",
        "result-created",
    )
    assert {
        "idx_benchmark_runs_scenario",
        "idx_benchmark_runs_status",
        "idx_benchmark_results_runs",
    }.issubset(_schema_names(init_db_path, "index"))
    assert quick_check == "ok"
