"""Every landed write owes its own publication row; the lane bounds how many.

Rows are never merged or rewritten: each keeps its write's prior mirror, so
owner events and registration replay exactly, one row at a time.
"""
from contextlib import closing
import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest

from doceditor.live_publication_record import Publication
from doceditor.live_store import LiveStore, PendingWrite
from doceditor.live_text import clone, create
from registry import service
from registry.identity_metadata import parse_metadata
from registry.shared_documents import resolve_shared_target
from tests.test_document_live_publisher import setup, events, owed
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

PLAIN = {"path": "/workspace/doc.md", "plain": True}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def land(store: LiveStore, text: str, *, actor="actor", decision=PLAIN) -> None:
    """Land one lane write whose prior is the promoted mirror, as the lane does."""
    mirror = store.mirror()
    new = create(text).get_update()
    publication = Publication(actor, None, "flush", mirror.state, decision)
    record = PendingWrite(uuid4().hex, new, new, mirror.file_hash, digest(text), publication=publication)
    store.record_pending(record)
    store.promote(record.op_id)


def fresh(tmp_path) -> LiveStore:
    store = LiveStore(tmp_path / "store.sqlite")
    store.initialize(create("zero").get_update(), digest("zero"))
    return store


def text_of(state: bytes) -> str:
    return str(clone(state)["text"])


def drain(store: LiveStore) -> list:
    rows = []
    while (row := store.next_publication()) is not None:
        rows.append(row)
        store.delete_publication(row.seq)
    return rows


def test_every_landed_write_owes_its_own_row_chained_by_prior(tmp_path):
    store = fresh(tmp_path)
    for index in range(20):
        land(store, f"text {index}", actor="a" if index % 2 else "b")
    rows = drain(store)
    assert len(rows) == 20
    assert text_of(rows[0].publication.prior_mirror) == "zero"
    for earlier, later in zip(rows, rows[1:]):
        assert later.publication.prior_mirror == earlier.mirror_state
    assert rows[-1].result_hash == digest("text 19")


def test_owed_rows_are_never_rewritten(tmp_path):
    """No row merges: the publish table has no run key, and each row keeps its own write."""
    store = fresh(tmp_path)
    with closing(sqlite3.connect(store.path)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(publish)")}
    assert "run_key" not in columns
    assert not hasattr(Publication, "run_key")
    for _ in range(3):
        land(store, "same text")
    assert store.owed_count() == 3
    assert [row.result_hash for row in drain(store)] == [digest("same text")] * 3


@pytest.mark.asyncio
async def test_drain_reads_one_owed_row_per_pass(tmp_path, monkeypatch):
    from doceditor import live_publisher
    store = fresh(tmp_path)
    for actor in ("a", "b", "c"):
        land(store, actor, actor=actor)
    reads, published = [], []
    real = store._read

    def spy(query, args=()):
        rows = real(query, args)
        if "FROM publish" in query:
            reads.append(len(rows))
        return rows

    def success(store, row, **kwargs):
        published.append(row.publication.actor)
        store.delete_publication(row.seq)

    monkeypatch.setattr(store, "_read", spy)
    monkeypatch.setattr(live_publisher, "publish_one", success)
    publisher = live_publisher.DocumentPublisher(store)
    try:
        await publisher._drain()
    finally:
        await publisher.close()
    assert published == ["a", "b", "c"]
    assert reads == [1, 1, 1, 0]


def owe_shared(scope, store, text):
    """Land a shared write decided against the promoted mirror, as the lane does."""
    path = scope["category"] / "live.md"
    mirror = store.mirror()
    target = resolve_shared_target(path, workspace=scope["workspace"], thread_id=scope["conductor"],
                                   project_id="project-alpha", description=None, direct_user=False,
                                   proposed_content=text.encode(), db=scope["db"], setup=False,
                                   promoted_hash=mirror.file_hash)
    new = create(text).get_update()
    publication = Publication(scope["conductor"], None, None, mirror.state, Publication.decision_for(path, target))
    store.record_pending(PendingWrite(text, new, new, mirror.file_hash, digest(text), publication=publication))
    path.write_text(text)
    store.promote(text)


def assert_run_published(scope, store):
    diffs = [json.loads(row[0])["diff"] for row in events(scope)]
    assert len(diffs) == 3
    assert "-old" in diffs[0] and "+first" in diffs[0]
    assert "-first" in diffs[1] and "+second" in diffs[1]
    assert "-second" in diffs[2] and "+third" in diffs[2]
    artifact = service.find_file_identity(str(scope["category"] / "live.md"), db=scope["db"])
    metadata = parse_metadata(artifact["metadata"])
    assert metadata["last_written_sha256"] == digest("third")
    with closing(sqlite3.connect(store.path)) as db:
        latest = db.execute("SELECT MAX(seq) FROM mirror").fetchone()[0]
    assert metadata["live_publication"] == {"store_id": store.store_id, "seq": latest}
    assert store.next_publication() is None


def test_each_owed_row_publishes_once_with_the_latest_stamp(ownership_scope, tmp_path):
    from doceditor.live_publisher import publish_one
    scope = ownership_scope
    store = setup(scope, tmp_path)
    for text in ("first", "second", "third"):
        owe_shared(scope, store, text)
    assert len(owed(store)) == 3
    for _ in range(3):
        publish_one(store, store.next_publication(), db=scope["db"])
    assert_run_published(scope, store)


def test_crash_after_commit_publishes_each_row_once_from_a_reopened_store(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_publisher import publish_one
    scope = ownership_scope
    store = setup(scope, tmp_path)
    for text in ("first", "second", "third"):
        owe_shared(scope, store, text)
    reopened = LiveStore(store.path)
    publish_one(reopened, reopened.next_publication(), db=scope["db"])

    def cut(seq):
        raise KeyboardInterrupt("killed after the application commit")

    monkeypatch.setattr(reopened, "delete_publication", cut)
    with pytest.raises(KeyboardInterrupt):
        publish_one(reopened, reopened.next_publication(), db=scope["db"])
    restarted = LiveStore(store.path)
    while (row := restarted.next_publication()) is not None:
        publish_one(restarted, row, db=scope["db"])
    assert_run_published(scope, restarted)
