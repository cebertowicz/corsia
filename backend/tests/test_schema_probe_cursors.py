"""A schema probe that stops reading early never leaves its statement pinning a read snapshot.

The probes answer from the first matching row. If the cursor they iterate is
still stepping and something keeps it alive past the call (a traceback, or the
CPU sampler's stack snapshot holding the frame), the connection keeps its read
snapshot, and its next ``BEGIN IMMEDIATE`` fails with SQLITE_BUSY_SNAPSHOT once
another connection commits. The connection here keeps every cursor it hands
out, which is the worst case of that lifetime.
"""

import sqlite3

import pytest

from plans import aggregates, node_states


class _KeepsCursors(sqlite3.Connection):
    """Holds a reference to every cursor, as a sampled stack frame would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kept: list[sqlite3.Cursor] = []

    def execute(self, sql, parameters=(), /):
        cursor = self.cursor().execute(sql, parameters)
        self.kept.append(cursor)
        return cursor


@pytest.fixture
def connections(tmp_path):
    path = tmp_path / "probe.db"
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    # The probed column comes first, so a probe that stops at it leaves rows unread.
    setup.execute("CREATE TABLE plan_nodes (sub_open INT, later INT, a INT, b INT, c INT)")
    setup.execute("CREATE TABLE other (x INT)")
    setup.commit()
    setup.close()
    probe = sqlite3.connect(path, factory=_KeepsCursors, isolation_level=None)
    writer = sqlite3.connect(path, isolation_level=None)
    yield probe, writer
    probe.close()
    writer.close()


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(aggregates.ready, id="aggregates.ready"),
        pytest.param(node_states.later_ready, id="node_states.later_ready"),
    ],
)
def test_probe_leaves_no_snapshot_for_the_next_write(connections, probe):
    probe_db, writer = connections
    assert probe(probe_db)
    writer.execute("INSERT INTO other VALUES (1)")

    probe_db.execute("BEGIN IMMEDIATE")
    probe_db.execute("INSERT INTO other VALUES (2)")
    probe_db.execute("COMMIT")

    assert all(cursor.fetchone() is None for cursor in probe_db.kept)


def test_a_probe_inside_a_write_transaction_leaves_no_snapshot_after_its_commit(tmp_path):
    """Being inside the caller's write is no exemption: a kept cursor outlives the COMMIT."""
    from connections.harness_legacy_fallback import sync_legacy_fallback
    from connections.harness_model_rows import CACHE_KEY

    path = tmp_path / "fallback.db"
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    # harness is the second column, so a probe that stops at it leaves rows unread.
    setup.execute("CREATE TABLE models (id TEXT, harness TEXT, source TEXT, provider_model_id TEXT, a INT, b INT)")
    setup.execute("CREATE TABLE other (x INT)")
    setup.commit()
    setup.close()
    probe_db = sqlite3.connect(path, factory=_KeepsCursors, isolation_level=None)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        probe_db.execute("BEGIN IMMEDIATE")
        sync_legacy_fallback("codex", config={CACHE_KEY: {}}, db=probe_db)
        probe_db.execute("COMMIT")
        writer.execute("INSERT INTO other VALUES (1)")

        probe_db.execute("BEGIN IMMEDIATE")
        probe_db.execute("INSERT INTO other VALUES (2)")
        probe_db.execute("COMMIT")

        assert all(cursor.fetchone() is None for cursor in probe_db.kept)
    finally:
        probe_db.close()
        writer.close()
