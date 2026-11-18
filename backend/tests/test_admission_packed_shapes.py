"""Packed read shape lifetime follows the admission transaction frame."""

import sqlite3

import pytest

from infra.database import write_transaction
from infra.packed_rows import fetch_all


@pytest.mark.parametrize("roll_back", [False, True])
def test_shapes_reused_only_within_admission_frame(roll_back):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE t (old TEXT)")
    db.execute("INSERT INTO t VALUES ('value')")
    db.commit()
    statements = []
    db.set_trace_callback(statements.append)

    def read_twice():
        assert [dict(row) for row in fetch_all(db, "SELECT * FROM t")] == [{"old": "value"}]
        assert [dict(row) for row in fetch_all(db, "SELECT * FROM t")] == [{"old": "value"}]

    if roll_back:
        with pytest.raises(RuntimeError, match="rollback"):
            with write_transaction(db, scope="admit_input"):
                read_twice()
                raise RuntimeError("rollback")
    else:
        with write_transaction(db, scope="admit_input"):
            read_twice()
    assert sum("LIMIT 0" in sql for sql in statements) == 1
    db.execute("ALTER TABLE t RENAME COLUMN old TO new")
    db.commit()
    with write_transaction(db, scope="admit_input"):
        assert [dict(row) for row in fetch_all(db, "SELECT * FROM t")] == [{"new": "value"}]
    assert sum("LIMIT 0" in sql for sql in statements) == 2
    db.close()


def test_nested_admission_discards_shapes_and_preserves_row_factory():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE t (name TEXT)")
    db.execute("INSERT INTO t VALUES ('value')")
    db.commit()
    statements = []
    db.set_trace_callback(statements.append)
    with write_transaction(db, scope="outer"):
        with write_transaction(db, scope="admit_input"):
            assert fetch_all(db, "SELECT * FROM t")[0]["name"] == "value"
            db.row_factory = None
            assert fetch_all(db, "SELECT * FROM t") == [("value",)]
            db.row_factory = lambda cursor, row: tuple(row)
            # A user factory may use the cursor itself; it receives a fresh one.
            assert fetch_all(db, "SELECT * FROM t") == [("value",)]
        db.row_factory = sqlite3.Row
        assert fetch_all(db, "SELECT * FROM t")[0]["name"] == "value"
    assert sum("LIMIT 0" in sql for sql in statements) == 3
    db.close()
