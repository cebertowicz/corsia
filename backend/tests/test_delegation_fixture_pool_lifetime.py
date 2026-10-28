"""The delegation fixture must leave the DB pool as clean as it found it.

``deleg_db`` isolates BELOW ``get_db()``: it redirects ``DB_PATH`` and resets the
thread-local pool so every module reads its database, with no per-module
allowlist to maintain. That boundary has a lifetime the old front-of-``get_db``
patching did not: ``monkeypatch`` restores ``DB_PATH`` at teardown, but it knows
nothing about the pooled connection opened while the redirect was live. Left
behind, that connection stays bound to a ``tmp_path`` database pytest later
deletes, and the next test on the same worker thread silently reads it.

These tests are order-dependent by construction and must stay in this order.
"""

from __future__ import annotations

from delegation_fixtures import deleg_db  # noqa: F401 - fixture import


def _backing_file(conn) -> str:
    """Return the file a live sqlite connection is actually reading."""
    raw = getattr(conn, "_conn", conn)
    return raw.execute("PRAGMA database_list").fetchall()[0][2] or ""


def test_deleg_db_opens_a_pooled_connection_to_its_own_database(deleg_db):  # noqa: F811
    """Establish the precondition: the fixture really did move the pool."""
    from infra import database

    assert _backing_file(database.get_db()) == str(database.DB_PATH)
    assert "deleg.db" in _backing_file(deleg_db)


def test_deleg_db_keeps_the_shared_db_bridge_unpatched(deleg_db):  # noqa: F811
    """Modules imported during the fixture must not retain a temporary bridge."""
    from infra import db_async, thread_execution

    assert db_async.run_in_db.__module__ == "infra.db_async"
    assert thread_execution.run_in_db is db_async.run_in_db


def test_the_next_test_does_not_inherit_that_connection():
    """The leak: this test never asked for the delegation database.

    Without a teardown reset it gets the previous test's pooled connection,
    still bound to a ``tmp_path`` file that no longer belongs to it -- and that
    pytest may already have deleted.
    """
    from infra import database

    actual = _backing_file(database.get_db())
    assert "deleg.db" not in actual, (
        f"pooled connection leaked from deleg_db: reading {actual!r}"
    )
    assert actual == str(database.DB_PATH), (
        f"pooled connection reads {actual!r}, but DB_PATH is "
        f"{str(database.DB_PATH)!r}"
    )
