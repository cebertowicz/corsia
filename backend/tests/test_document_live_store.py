"""Durable document rows keep exact batches and immutable held evidence."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest
from pycrdt import Doc, Text


def state(text: str) -> bytes:
    doc: Doc = Doc()
    doc["text"] = Text(text)
    return doc.get_update()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def seeded(tmp_path):
    from doceditor.live_store import LiveStore

    store = LiveStore(tmp_path / "document.sqlite")
    initial = state("hello")
    store.initialize(initial, digest("hello"))
    return store, initial


def reconstructed(store) -> str:
    doc: Doc = Doc()
    doc["text"] = Text()
    for update in store.load_updates():
        doc.apply_update(update)
    return str(doc["text"])


def pending(initial):
    from doceditor.live_store import PendingWrite

    fork = Doc()
    fork["text"] = Text()
    fork.apply_update(initial)
    before = fork.get_state()
    fork["text"].insert(5, " agent")
    return PendingWrite("op", fork.get_update(before), fork.get_update(), digest("hello"), digest("hello agent"))


def test_store_identity_survives_reopen_and_initialization(seeded):
    from doceditor.live_store import LiveStore

    store, initial = seeded
    reopened = LiveStore(store.path)
    reopened.initialize(state("other"), digest("other"))
    assert reopened.store_id == store.store_id
    assert reopened.load_updates() == (initial,)
    assert reopened.mirror().state == initial
    assert reconstructed(reopened) == "hello"


def test_deletion_batch_is_durable_and_ack_names_messages(seeded):
    from doceditor.live_store import BrowserMessage, LiveStore

    store, initial = seeded
    browser = Doc()
    browser["text"] = Text()
    browser.apply_update(initial)
    before = browser.get_state()
    del browser["text"][0:1]
    assert browser.get_state() == before
    deletion = BrowserMessage("delete", browser.get_update(before))
    assert store.commit_batch((deletion,)) == ("delete",)
    assert reconstructed(LiveStore(store.path)) == "ello"
    assert store.mirror().file_hash == digest("hello")


def test_record_carries_batch_atomically_and_promote_uses_ids(seeded):
    from doceditor.live_store import BrowserMessage

    store, initial = seeded
    browser = Doc()
    browser["text"] = Text()
    browser.apply_update(initial)
    before = browser.get_state()
    browser["text"].insert(0, "USER ")
    batch = (BrowserMessage("m1", browser.get_update(before)),)
    record = pending(initial)
    assert store.record_pending(record, batch) == ("m1",)
    assert reconstructed(store) == "USER hello"
    assert store.pending() == record
    store.promote("op")
    assert reconstructed(store) == "USER hello agent"
    assert store.pending() is None
    assert store.mirror().state == record.mirror_state


def test_second_pending_refused(seeded):
    from doceditor.live_store import StoreRefused

    store, initial = seeded
    record = pending(initial)
    store.record_pending(record)
    with pytest.raises(StoreRefused):
        store.record_pending(record)
    assert store.pending() == record
    assert store.load_updates() == (initial,)


def test_held_original_bytes_survive_choice_record_and_clear(seeded):
    from doceditor.live_store import HeldChoice

    store, initial = seeded
    record = pending(initial)
    store.record_pending(record)
    store.mark_held("op", digest("outside"))
    held = store.pending()
    choice = HeldChoice("corsia", b"choice-update", b"choice-state", digest("outside"), digest("chosen"))
    store.record_choice("op", choice)
    assert store.pending().candidate == held.candidate
    assert store.pending().mirror_state == held.mirror_state
    assert store.pending().prior_hash == held.prior_hash
    assert store.pending().result_hash == held.result_hash
    assert store.pending().choice == choice
    store.clear_choice("op")
    assert store.pending() == held


def test_held_row_cannot_be_discarded_or_rerecorded(seeded):
    from doceditor.live_store import StoreRefused

    store, initial = seeded
    store.record_pending(pending(initial))
    store.mark_held("op", digest("outside"))
    held = store.pending()
    with pytest.raises(StoreRefused):
        store.discard("op")
    with pytest.raises(StoreRefused):
        store.record_pending(pending(initial))
    assert store.pending() == held


def test_choice_transactions_carry_received_updates(seeded):
    from doceditor.live_store import BrowserMessage, HeldChoice

    store, initial = seeded
    store.record_pending(pending(initial))
    store.mark_held("op", digest("outside"))
    browser = Doc()
    browser["text"] = Text()
    browser.apply_update(initial)
    before = browser.get_state()
    browser["text"].insert(0, "BIG ")
    batch = (BrowserMessage("m1", browser.get_update(before)),)
    choice = HeldChoice("corsia", browser.get_update(browser.get_state()), browser.get_update(), digest("outside"), digest("BIG hello"))
    assert store.record_choice("op", choice, batch) == ("m1",)
    store.apply_choice("op", choice)
    assert reconstructed(store) == "BIG hello"
    assert store.pending() is None
    assert store.mirror().file_hash == digest("BIG hello")


def test_refused_clear_is_real_sqlite_refusal_and_preserves_evidence(seeded, monkeypatch):
    from doceditor.live_store import HeldChoice, StoreRefused

    store, initial = seeded
    store.record_pending(pending(initial))
    store.mark_held("op", digest("outside"))
    store.record_choice("op", HeldChoice("corsia", b"x", b"y", "prior", "result"))
    evidence = store.pending()
    original_connect = store._connect

    def read_only():
        connection = original_connect()
        connection.execute("PRAGMA query_only=ON")
        return connection

    monkeypatch.setattr(store, "_connect", read_only)
    with pytest.raises(StoreRefused, match="readonly"):
        store.clear_choice("op")
    assert store.pending() == evidence


def test_refused_record_does_not_partially_commit_batch(seeded, monkeypatch):
    from doceditor.live_store import BrowserMessage, StoreRefused

    store, initial = seeded
    record = pending(initial)
    store.record_pending(record)
    with pytest.raises(StoreRefused):
        store.record_pending(record, (BrowserMessage("m1", state("foreign")),))
    assert store.load_updates() == (initial,)


def test_one_second_busy_bound_and_full_durability(seeded):
    store, _ = seeded
    with store._connect() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {"meta", "live", "mirror", "pending", "publish"}


def test_wrong_operation_cannot_retire_pending(seeded):
    from doceditor.live_store import StoreRefused

    store, initial = seeded
    store.record_pending(pending(initial))
    with pytest.raises(StoreRefused):
        store.promote("other")
    assert store.pending().op_id == "op"
    assert reconstructed(store) == "hello"


def test_uncleared_choice_cannot_be_replaced_by_another_choice(seeded):
    from doceditor.live_store import HeldChoice, StoreRefused

    store, initial = seeded
    store.record_pending(pending(initial))
    store.mark_held("op", "outside")
    choice = HeldChoice("corsia", b"original", b"mirror", "prior", "result")
    store.record_choice("op", choice)
    evidence = store.pending()
    replacement = HeldChoice("file", b"replacement", b"other", "prior", "new")
    with pytest.raises(StoreRefused):
        store.apply_choice("op", replacement)
    assert store.pending() == evidence
    assert store.load_updates() == (initial,)
