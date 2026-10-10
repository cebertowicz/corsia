"""A real killed publisher cannot duplicate ordered events or regress hashes."""
import asyncio
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
import time
import threading

import pytest

from doceditor.live_store import LiveStore
from registry import service
from registry.identity_metadata import parse_metadata
from tests.test_document_live_publisher import setup, owe, events
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_killed_after_second_stamp_recovers_each_event_once(ownership_scope, tmp_path):
    from doceditor.live_publisher import publish_one
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "first")
    owe(scope, store, "first", "second")
    # A real app writer refuses the first publisher without affecting either file write.
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    import sqlite3
    lock = sqlite3.connect(db_path)
    lock.execute("BEGIN IMMEDIATE")
    scope["db"].execute("PRAGMA busy_timeout=100")
    with pytest.raises(sqlite3.OperationalError):
        publish_one(store, store.next_publication(), db=scope["db"])
    lock.rollback()
    lock.close()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), str(scope["workspace"]),
        str(db_path), str(store.path), cwd=tmp_path, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        # A crashed child ends the line at EOF; this bound only stops a hang. A cold
        # backend import in the child alone has taken over 10 seconds on a loaded host.
        line = await asyncio.wait_for(process.stdout.readline(), 120)
        if line != b"CUT\n":
            _, error = await process.communicate()
            pytest.fail(error.decode())
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert len(events(scope)) == 2
    reopened = LiveStore(store.path)
    last = reopened.next_publication()
    publish_one(reopened, last, db=scope["db"])
    assert reopened.next_publication() is None
    assert len(events(scope)) == 2
    artifact = service.find_file_identity(str(scope["category"] / "live.md"), db=scope["db"])
    assert parse_metadata(artifact["metadata"])["last_written_sha256"] == hashlib.sha256(b"second").hexdigest()
    import json
    diffs = [json.loads(row[0])["diff"] for row in events(scope)]
    assert any("-old" in diff and "+first" in diff for diff in diffs)
    assert any("-first" in diff and "+second" in diff for diff in diffs)


def test_maximum_publication_prepares_files_store_and_diff_before_begin(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher
    from doceditor.document_scope import MAX_TEXT_BYTES
    from registry import shared_document_events
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "x" * MAX_TEXT_BYTES)
    row = store.next_publication()
    inside = False
    holds = []
    real_transaction = live_publisher.write_transaction

    @contextmanager
    def measured(db, *, scope):
        nonlocal inside
        with real_transaction(db, scope=scope) as connection:
            inside = True
            start = time.perf_counter()
            try:
                yield connection
            finally:
                inside = False
        holds.append(time.perf_counter() - start)

    def guarded(original):
        def call(*args, **kwargs):
            assert not inside, "File/store/content-sized preparation under publisher BEGIN"
            return original(*args, **kwargs)
        return call

    monkeypatch.setattr(live_publisher, "write_transaction", measured)
    monkeypatch.setattr(Path, "read_bytes", guarded(Path.read_bytes))
    monkeypatch.setattr(Path, "stat", guarded(Path.stat))
    monkeypatch.setattr(Path, "resolve", guarded(Path.resolve))
    monkeypatch.setattr(store, "_connect", guarded(store._connect))
    monkeypatch.setattr(shared_document_events, "capped_unified_diff", guarded(shared_document_events.capped_unified_diff))
    monkeypatch.setattr(shared_document_events, "classify_edit", guarded(shared_document_events.classify_edit))
    live_publisher.publish_one(store, row, db=scope["db"])
    assert len(holds) == 1 and holds[0] < 1.0
    print(f"maximum-publication-writer-hold={holds[0]:.4f}s")


def test_closed_feedback_segment_is_combined_before_publisher_begin(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher, feedback_edits, feedback_edit_preparation
    from registry import shared_edit_classification
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "first")
    live_publisher.publish_one(store, store.next_publication(), db=scope["db"])
    owe(scope, store, "outside", "second")
    inside = False
    calls = []
    real_transaction = live_publisher.write_transaction
    @contextmanager
    def measured(db, *, scope):
        nonlocal inside
        with real_transaction(db, scope=scope) as connection:
            inside = True
            try:
                yield connection
            finally:
                inside = False
    original = shared_edit_classification.combine_capped_diffs
    def guarded(*args, **kwargs):
        assert not inside, "Feedback segment combination under publisher BEGIN"
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(live_publisher, "write_transaction", measured)
    monkeypatch.setattr(shared_edit_classification, "combine_capped_diffs", guarded)
    monkeypatch.setattr(feedback_edits, "combine_capped_diffs", guarded)
    monkeypatch.setattr(feedback_edit_preparation, "combine_capped_diffs", guarded, raising=False)
    live_publisher.publish_one(store, store.next_publication(), db=scope["db"])
    assert calls == [True]
    assert len(events(scope)) == 2


def worker(workspace, db_path, store_path):
    from infra import config, database
    from infra.platform_paths import WorkspaceSelection
    from doceditor.live_publisher import publish_one
    config.WORKSPACE = workspace
    config.WORKSPACE_SELECTION = WorkspaceSelection(workspace_id="workspace-stable-id", root=workspace, name="Test")
    config.DB_PATH = database.DB_PATH = db_path
    service.get_allowed_dirs_rw = lambda **kwargs: [workspace]
    store = LiveStore(store_path)
    publish_one(store, store.next_publication())
    second = store.next_publication()
    def park(seq):
        print("CUT", flush=True)
        threading.Event().wait()
    store.delete_publication = park
    publish_one(store, second)


if __name__ == "__main__":
    worker(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
