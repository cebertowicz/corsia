"""Retrieval never crosses an owner boundary.

Checkpoint narratives are among the most concentrated records a thread holds:
what was decided, what was rejected, and why. Retrieval reads them across
threads by design, so the owner check is the invariant that keeps a
cross-thread convenience from becoming a cross-user disclosure.

Every entry point is covered here, not just the first one. An id-addressed
read is the path most easily assumed safe -- the id looks like a secret and
the row names its own thread -- but naming an owner is identity, not
permission, so it gets the same test as search.

The caller's identity is always resolved from the backend-injected calling
thread. A user-supplied user id would let the model assert whose data it is
reading, which is the vulnerability rather than the fix.
"""

from __future__ import annotations

from infra.thread_confinement import Reader

import asyncio

from checkpoint_retrieval_fixtures import (  # noqa: F401
    OTHER_USER,
    OWNER_USER,
    _install_checkpoint,
    fresh_db,
)

FOREIGN_NARRATIVE = "We chose the private vendor terms and rejected the rest."


def test_search_excludes_another_users_generations(fresh_db):
    """A matching generation in a foreign thread is not a search result."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="We chose event routing."
    )
    _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative=FOREIGN_NARRATIVE
    )

    hits = search_checkpoints(fresh_db, "chose", reader=Reader.agent("t1"), limit=5)

    assert [h["thread_id"] for h in hits] == ["t1"]


def test_search_scoped_to_a_foreign_thread_returns_nothing(fresh_db):
    """Naming another user's thread as the filter does not widen access."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative=FOREIGN_NARRATIVE
    )

    hits = search_checkpoints(
        fresh_db, "vendor", reader=Reader.agent("t1"), thread_id="t-foreign"
    )

    assert hits == []


def test_search_logs_an_unavailable_thread_without_disclosing_it(fresh_db):
    """The operator can distinguish a denied filter from a real empty search."""
    from loguru import logger
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative=FOREIGN_NARRATIVE
    )
    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        hits = search_checkpoints(
            fresh_db, "vendor", reader=Reader.agent("t1"), thread_id="t-foreign"
        )
    finally:
        logger.remove(sink)

    assert hits == []
    assert [record for record in records if "checkpoint_search reason=" in record] == [
        "checkpoint_search reason=unavailable_thread hits=0 current_hits=0 historical_hits=0\n"
    ]


def test_recent_refuses_another_users_thread(fresh_db):
    """Listing a foreign thread's recent generations returns nothing."""
    from memory.checkpoint_search import recent_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative=FOREIGN_NARRATIVE
    )

    assert recent_checkpoints(
        fresh_db, "t-foreign", reader=Reader.agent("t1")
    ) == []


def test_exact_read_refuses_another_users_generation(fresh_db):
    """A known id does not read across an owner boundary.

    The response is the same unavailable answer a missing id gets: the read
    must not become an oracle that confirms the id exists.
    """
    from memory.checkpoint_search import UNAVAILABLE_MESSAGE, get_checkpoint

    cid = _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative=FOREIGN_NARRATIVE
    )

    got = get_checkpoint(fresh_db, cid, reader=Reader.agent("t1"))
    missing = get_checkpoint(
        fresh_db, "cp-does-not-exist", reader=Reader.agent("t1")
    )

    assert got["error_code"] == "not_found"
    assert got["message"] == UNAVAILABLE_MESSAGE
    assert got == missing
    assert "vendor" not in str(got)


def test_unknown_caller_thread_reads_nothing(fresh_db):
    """An unresolvable caller fails closed rather than reading everything."""
    from memory.checkpoint_search import (
        get_checkpoint,
        recent_checkpoints,
        search_checkpoints,
    )

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="We chose event routing."
    )

    assert search_checkpoints(fresh_db, "chose", reader=Reader.agent("ghost")) == []
    assert recent_checkpoints(fresh_db, "t1", reader=Reader.agent("ghost")) == []
    assert get_checkpoint(fresh_db, cid, reader=Reader.agent("ghost"))[
        "error_code"
    ] == "not_found"


def test_owner_still_reads_their_own_generations(fresh_db):
    """The guard denies only foreign rows; the owner's reads are unchanged."""
    from memory.checkpoint_search import (
        get_checkpoint,
        recent_checkpoints,
        search_checkpoints,
    )

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="We chose event routing."
    )

    assert len(search_checkpoints(fresh_db, "chose", reader=Reader.agent("t1"))) == 1
    assert len(recent_checkpoints(fresh_db, "t1", reader=Reader.agent("t1"))) == 1
    assert get_checkpoint(fresh_db, cid, reader=Reader.agent("t1"))[
        "checkpoint_id"
    ] == cid


def _run_tool(fresh_db, **params) -> str:
    """Execute the retrieval tool against the in-memory database."""
    import tools.query_checkpoints as tool_module
    from infra import database
    from tools.query_checkpoints import GetCheckpointsTool

    async def _inline(fn):
        return fn()

    class _KeepOpen:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            pass

    original_db, original_run = database.get_db, tool_module.run_in_db
    database.get_db = lambda: _KeepOpen(fresh_db)
    tool_module.run_in_db = _inline
    try:
        return asyncio.run(GetCheckpointsTool().execute(**params))
    finally:
        database.get_db = original_db
        tool_module.run_in_db = original_run


def test_tool_reads_foreign_generation_as_unavailable(fresh_db):
    """End to end, the tool will not surface another user's narrative."""
    cid = _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative=FOREIGN_NARRATIVE
    )

    out = _run_tool(fresh_db, checkpoint_id=cid, _thread_id="t1")

    assert "vendor" not in out
    assert "error_code=not_found" in out


def test_tool_without_injected_caller_refuses(fresh_db):
    """No backend-injected identity means no read, rather than an open read."""
    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="We chose event routing."
    )

    out = _run_tool(fresh_db, checkpoint_id=cid)

    assert "event routing" not in out
    assert "Error:" in out


def test_exact_tool_labels_only_older_epochs_historical(fresh_db):
    """Current trajectory is not mislabeled while old project epochs are."""
    current = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Current routing.", checkpoint_id="cp-current"
    )
    assert "historical epoch" not in _run_tool(
        fresh_db, checkpoint_id=current, _thread_id="t1"
    )

    fresh_db.execute("UPDATE threads SET history_epoch=1 WHERE id='t1'")
    fresh_db.commit()

    historical = _run_tool(fresh_db, checkpoint_id=current, _thread_id="t1")
    assert "historical epoch: 0" in historical


def _run_search(fresh_db, **params) -> str:
    """Execute the history-search tool against the in-memory database."""
    import tools.query_search as search_module
    from infra import database
    from tools.query_search import SearchHistoryTool

    async def _inline(fn):
        return fn()

    class _KeepOpen:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            pass

    original_db, original_run = database.get_db, search_module.run_in_db
    database.get_db = lambda: _KeepOpen(fresh_db)
    search_module.run_in_db = _inline
    try:
        return asyncio.run(SearchHistoryTool().execute(**params))
    finally:
        database.get_db = original_db
        search_module.run_in_db = original_run


def test_search_tool_labels_only_older_epoch_as_historical(fresh_db):
    """The search presentation reserves historical for an older epoch."""
    fresh_db.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT,"
        " role TEXT, content TEXT, metadata TEXT, created_at TEXT, delivery_state TEXT)"
    )
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Current event routing.", checkpoint_id="cp-current"
    )
    fresh_db.commit()

    current = _run_search(
        fresh_db, query="event", include_checkpoints=True, _thread_id="t1"
    )
    assert "historical epoch" not in current

    fresh_db.execute("UPDATE threads SET history_epoch=1 WHERE id='t1'")
    fresh_db.commit()
    historical = _run_search(
        fresh_db, query="event", include_checkpoints=True, _thread_id="t1"
    )
    assert "historical epoch: 0" in historical


def test_search_reports_omitted_checkpoints_rather_than_hiding_them(fresh_db):
    """An unauthorizable checkpoint half is reported, never silently dropped.

    Returning only message results would let a caller read "no checkpoint
    matches" from a search that never ran one, which is the failure mode a
    combined result surface must not have.
    """
    # The combined tool also searches messages; retrieval's own fixture has no
    # message history, so supply the empty table that half needs.
    fresh_db.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT,"
        " role TEXT, content TEXT, metadata TEXT, created_at TEXT, delivery_state TEXT)"
    )
    fresh_db.commit()
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="We chose event routing."
    )

    out = _run_search(fresh_db, query="chose", include_checkpoints=True)

    assert "NOT searched" in out
    assert "missing, not empty" in out
    assert "event routing" not in out


def test_search_result_presentation_explains_canonical_lifecycle_state():
    """The tool exposes the revision and retirement reason carried by hits."""
    from tools.query_search import _format_checkpoint_results

    common = {
        "kind": "continuity_entry",
        "thread_id": "t1",
        "entry_id": 7,
        "field": "facts",
        "statement": "A retained conclusion.",
        "first_checkpoint": None,
        "latest_checkpoint": None,
        "historical": False,
        "history_epoch": 0,
    }
    revised = _format_checkpoint_results(
        "Messages", "retained", [{
            **common,
            "entry_state": "revised",
            "current_revision": 3,
        }]
    )
    retired = _format_checkpoint_results(
        "Messages", "retained", [{
            **common,
            "entry_state": "retired",
            "retired_reason": "superseded",
        }]
    )

    assert "state: revised | current revision: 3" in revised
    assert "state: retired | retired reason: superseded" in retired
