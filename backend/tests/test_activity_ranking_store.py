"""Scope ranks replace atomically and only affect currently visible items."""

import sqlite3

import pytest

from infra.activity_ranking import apply_ranks, replace_ranks, validate_order
from infra.activity_ranking_schema import ACTIVITY_RANKING_SQL


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(ACTIVITY_RANKING_SQL)
    yield connection
    connection.close()


def test_ranks_order_current_items_and_ignore_stale(db):
    replace_ranks(db, "u", "workspace", "ranker", [{"key": "b", "reason": "First"}, {"key": "old", "reason": "Old"}], "2026-01-01")
    items = [{"key": "a"}, {"key": "b"}, {"key": "c"}]
    result = apply_ranks(db, "u", "workspace", items)
    assert [item["key"] for item in result] == ["b", "a", "c"]
    assert result[0]["rank"]["reason"] == "First"
    assert result[1]["rank"] is None


def test_replace_scopes_are_independent(db):
    replace_ranks(db, "u", "workspace", "ranker", [{"key": "a", "reason": "One"}], "2026-01-01")
    replace_ranks(db, "u", "project", "ranker", [{"key": "b", "reason": "Two"}], "2026-01-02")
    replace_ranks(db, "u", "workspace", "ranker", [{"key": "c", "reason": "Three"}], "2026-01-03")
    assert [row[0] for row in db.execute("SELECT item_key FROM activity_ranks WHERE scope='workspace'")] == ["c"]
    assert [row[0] for row in db.execute("SELECT item_key FROM activity_ranks WHERE scope='project'")] == ["b"]


def test_validation_names_stale_and_overlong_items():
    with pytest.raises(ValueError, match="Unknown key.*re-read"):
        validate_order([{"key": "gone", "reason": "Needed"}], {"a"})
    with pytest.raises(ValueError, match="a.*90"):
        validate_order([{"key": "a", "reason": "x" * 91}], {"a"})
    validate_order([{"key": "a", "reason": "x" * 90}], {"a", "b"})


def test_failed_replacement_rolls_back_to_previous_order(db):
    replace_ranks(db, "u", "workspace", "ranker", [{"key": "a", "reason": "One"}], "2026-01-01")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            replace_ranks(db, "u", "workspace", "ranker", [
                {"key": "b", "reason": "Two"}, {"key": "b", "reason": "Duplicate"},
            ], "2026-01-02")
    assert [row[0] for row in db.execute("SELECT item_key FROM activity_ranks")] == ["a"]
