"""Unreleased prototype stores are refused, never silently upgraded."""
import sqlite3
import pytest
from doceditor.live_store import LiveStore, StoreRefused


def test_unreleased_schema_does_not_gain_compatibility_columns(tmp_path):
    path = tmp_path / "prototype.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE pending (singleton INTEGER PRIMARY KEY, op_id TEXT, candidate BLOB, "
            "mirror_state BLOB, prior_hash TEXT, result_hash TEXT, state TEXT, outside_hash TEXT, "
            "choice TEXT, choice_candidate BLOB, choice_mirror BLOB, choice_prior_hash TEXT, choice_result_hash TEXT)")
    store = LiveStore(path)
    with pytest.raises(StoreRefused):
        store.pending()
    with sqlite3.connect(path) as db:
        assert "publication" not in {row[1] for row in db.execute("PRAGMA table_info(pending)")}
