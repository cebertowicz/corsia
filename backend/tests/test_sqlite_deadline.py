"""A read's deadline still stops a long query when checks are a million steps apart."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

from infra.sqlite_deadline import DEADLINE_CHECK_STEPS, stop_at

# Far more VM steps than any deadline in these tests allows: it never finishes on its own.
ENDLESS = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"


def test_a_query_past_its_deadline_is_interrupted_soon_after():
    assert DEADLINE_CHECK_STEPS == 1_000_000
    with closing(sqlite3.connect(":memory:")) as db:
        start = time.monotonic()
        stop_at(db, start + 0.2)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            db.execute(ENDLESS).fetchone()
        # One check interval is about 50 ms at production speed; the bound leaves room for slow hosts.
        assert time.monotonic() - start < 5.0


def test_a_query_inside_its_deadline_finishes():
    with closing(sqlite3.connect(":memory:")) as db:
        stop_at(db, time.monotonic() + 60)
        bounded = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 200000) SELECT count(*) FROM n"
        assert db.execute(bounded).fetchone()[0] == 200000
