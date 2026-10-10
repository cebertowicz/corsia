"""The lane's only journal owes ordered publication after successful promotion."""
import pytest

from doceditor.live_store import LiveStore, PendingWrite
from tests.test_document_live_publisher import owed
from tests.test_document_live_store import state, digest


def record(op, before, after):
    from doceditor.live_store import Publication
    return PendingWrite(op, state(after), state(after), digest(before), digest(after),
                        publication=Publication("actor", None, "description", state(before),
                                                {"owner_thread_id": "owner"}))


def test_pending_promote_atomically_owes_exact_bytes_and_monotonic_sequence(tmp_path):
    store = LiveStore(tmp_path / "store.sqlite")
    store.initialize(state("one"), digest("one"))
    pending = record("first", "one", "two")
    store.record_pending(pending)
    assert store.pending().publication == pending.publication
    assert store.next_publication() is None
    store.promote("first")
    first = store.next_publication()
    assert first.publication.actor == "actor"
    assert first.result_hash == digest("two")
    assert first.mirror_state == store.mirror().state
    store.delete_publication(first.seq)
    store.record_pending(record("second", "two", "three"))
    store.promote("second")
    second = store.next_publication()
    assert second.seq > first.seq
    reopened = LiveStore(store.path)
    assert reopened.next_publication() == store.next_publication()


def test_discarded_pending_never_owes_publication(tmp_path):
    store = LiveStore(tmp_path / "store.sqlite")
    store.initialize(state("one"), digest("one"))
    store.record_pending(record("first", "one", "two"))
    store.discard("first")
    assert store.next_publication() is None


def test_refused_promote_leaves_publication_in_pending_only(tmp_path):
    import sqlite3
    from doceditor.live_store import StoreRefused
    store = LiveStore(tmp_path / "store.sqlite")
    store.initialize(state("one"), digest("one"))
    store.record_pending(record("first", "one", "two"))
    original = store._connect
    def read_only():
        db = original()
        db.execute("PRAGMA query_only=ON")
        return db
    store._connect = read_only
    with pytest.raises(StoreRefused):
        store.promote("first")
    assert store.pending().publication.actor == "actor"
    assert store.next_publication() is None
    store._connect = original
    store.promote("first")
    assert len(owed(store)) == 1


def test_held_choice_owes_its_own_actor_without_rewriting_original_evidence(tmp_path):
    from doceditor.live_store import HeldChoice, Publication
    store = LiveStore(tmp_path / "store.sqlite")
    store.initialize(state("one"), digest("one"))
    pending = record("first", "one", "two")
    store.record_pending(pending)
    store.mark_held("first", digest("outside"))
    evidence = Publication("user", None, None, state("outside"), {"owner_thread_id": "owner"})
    choice = HeldChoice("corsia", state("chosen"), state("chosen"), digest("outside"),
                        digest("chosen"), publication=evidence)
    store.record_choice("first", choice)
    reopened = LiveStore(store.path)
    assert reopened.pending().publication == pending.publication
    assert reopened.pending().choice.publication == evidence
    reopened.apply_choice("first", reopened.pending().choice)
    assert reopened.next_publication().publication == evidence
