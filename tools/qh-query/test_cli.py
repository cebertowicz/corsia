"""Tests for the ``qh query`` CLI.

Sprint 5 Slice 0 (DECISIONS-LOG #26). Quick coverage of the four
modes (``--sql`` / ``--funnel`` / ``--before-after`` / ``--top-n``)
plus the TSV-vs-JSON output toggle.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
# Put the qh-query dir FIRST on sys.path so its ``cli`` shadows the
# backend ``cli.py`` (which is the corsia-cli setup-tool, unrelated).
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent.parent / "backend"))

# Disambiguate the import by loading the file directly via importlib —
# safer than relying on sys.path ordering when other test files also
# import backend.cli.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("qh_query_cli", _HERE / "cli.py")
assert _spec and _spec.loader
cli = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch, capsys):
    """Isolated DuckDB file with a few seed events."""
    from infra import analytics_db, config, product_events
    db_path = tmp_path / "analytics.duckdb"
    monkeypatch.setattr(config, "ANALYTICS_DB_PATH", db_path)
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", db_path)
    product_events._reset_for_tests()

    conn = analytics_db.get_db()
    import json as _json
    for ts, cat, act, props, sid in [
        (datetime(2026, 5, 1, 1, tzinfo=timezone.utc), "turn", "started",
         {"model": "haiku"}, "s1"),
        (datetime(2026, 5, 1, 2, tzinfo=timezone.utc), "turn", "completed",
         {"model": "haiku", "duration_ms": 100}, "s1"),
        (datetime(2026, 5, 2, 1, tzinfo=timezone.utc), "turn", "completed",
         {"model": "opus", "duration_ms": 800}, "s2"),
    ]:
        conn.execute(
            "INSERT INTO product_events VALUES (?, ?, ?, ?, ?, 'corsia')",
            (ts, cat, act, _json.dumps(props), sid),
        )
    yield db_path
    product_events._reset_for_tests()


def test_sql_mode_tsv(tmp_db, capsys):
    rc = cli.main(["--sql",
                   "SELECT category, COUNT(*) AS n FROM product_events GROUP BY category"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "category\tn" in out
    assert "turn\t3" in out


def test_sql_mode_json(tmp_db, capsys):
    rc = cli.main([
        "--sql",
        "SELECT category FROM product_events ORDER BY ts LIMIT 1",
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"category": "turn"}]


def test_funnel_mode(tmp_db, capsys):
    rc = cli.main([
        "--funnel", "turn.started,turn.completed",
        "--since", "all",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "turn.started\t1" in out
    # turn.completed sessions = 1 (only s1 reached both in order)
    assert "turn.completed\t1" in out


def test_top_n_mode(tmp_db, capsys):
    rc = cli.main(["--top-n", "category", "--since", "all", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0][0] == "turn"
    assert payload[0][1] == 3.0


def test_before_after_mode(tmp_db, capsys):
    rc = cli.main([
        "--before-after", "category=turn,action=completed",
        "--cutoff", "2026-05-02T00:00:00Z",
        "--metric", "properties->>'duration_ms'",
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["before"]["n"] == 1
    assert payload["after"]["n"] == 1
    assert payload["before"]["mean"] == 100.0
    assert payload["after"]["mean"] == 800.0


def test_before_after_requires_cutoff(tmp_db, capsys):
    rc = cli.main(["--before-after", "category=turn"])
    assert rc == 2


def test_unknown_since_format(tmp_db, capsys):
    rc = cli.main(["--top-n", "category", "--since", "two weeks"])
    assert rc == 2
