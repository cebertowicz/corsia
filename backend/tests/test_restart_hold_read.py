"""The restart-hold read on every Turn terminal: installed table, minimal fixture, other errors."""

from __future__ import annotations

import sqlite3

import pytest

from automation import self_restart


def _installed_db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    return db


def test_installed_table_reports_a_steering_hold() -> None:
    """With the table installed, no hold reads False and a steering operation reads True."""
    db = _installed_db()
    try:
        assert self_restart.restart_holds_new_work(db=db) is False
        operation_id = self_restart.try_acquire(None, None, None, None, "test update", db=db)
        assert operation_id is not None
        # ``holding`` reserves the operation without holding new work.
        assert self_restart.restart_holds_new_work(db=db) is False
        assert self_restart.transition_operation(
            operation_id, expected="holding", target="steering", db=db,
        )
        assert self_restart.restart_holds_new_work(db=db) is True
    finally:
        db.close()


def test_minimal_fixture_without_the_table_has_no_hold() -> None:
    """A database that never installed restart state cannot hold a cutoff."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        assert self_restart.restart_holds_new_work(db=db) is False
    finally:
        db.close()


class _Refusing:
    """A connection whose every statement fails with the given SQLite error."""

    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError(self.message)


@pytest.mark.parametrize(
    "message",
    ["database is locked", "no such table: some_other_table", "no such column: phase"],
)
def test_any_other_error_still_raises(message: str) -> None:
    """Only the missing restart table means no hold; a locked or broken database is not read as one."""
    with pytest.raises(sqlite3.OperationalError, match=message):
        self_restart.restart_holds_new_work(db=_Refusing(message))
