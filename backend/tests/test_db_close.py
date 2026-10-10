
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from infra.database import _NoOpClose  # noqa: E402


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_close_clears_lingering_transaction():
    raw = _make_conn()
    raw.execute("CREATE TABLE t (v INTEGER)")
    raw.commit()

    wrapped = _NoOpClose(raw)
    wrapped.execute("INSERT INTO t VALUES (1)")  # DML without commit
    assert raw.in_transaction, "DML should open an implicit txn"

    wrapped.close()

    assert not raw.in_transaction, "close() must clear the lingering txn"
    assert raw.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0, (
        "uncommitted write must be rolled back"
    )


def test_close_keeps_connection_alive_for_reuse():
    raw = _make_conn()
    wrapped = _NoOpClose(raw)
    wrapped.execute("CREATE TABLE t (id INTEGER)")
    wrapped.commit()
    wrapped.close()

    raw.execute("INSERT INTO t VALUES (7)")
    raw.commit()
    assert raw.execute("SELECT id FROM t").fetchone()[0] == 7


def test_close_after_commit_does_not_discard_data():
    raw = _make_conn()
    wrapped = _NoOpClose(raw)
    wrapped.execute("CREATE TABLE t (v INTEGER)")
    wrapped.execute("INSERT INTO t VALUES (42)")
    wrapped.commit()
    wrapped.close()

    assert raw.execute("SELECT v FROM t").fetchone()[0] == 42


def test_close_is_safe_on_clean_connection():
    raw = _make_conn()
    wrapped = _NoOpClose(raw)

    wrapped.close()  # nothing happened yet
    wrapped.close()  # idempotent

    wrapped.execute("CREATE TABLE t (id INTEGER)")
    wrapped.commit()
    wrapped.close()


def test_close_swallows_errors():
    raw = _make_conn()
    wrapped = _NoOpClose(raw)
    raw.close()  # real close — now rollback would error
    wrapped.close()  # must not raise
