"""Frames and durable callbacks use the already-loaded immutable store identity."""
from doceditor.live_store import LiveStore


def test_store_identity_does_not_reopen_sqlite_in_frame_callbacks(tmp_path, monkeypatch):
    store = LiveStore(tmp_path / "live.sqlite")
    expected = store.store_id
    def refused():
        raise AssertionError("Store identity opened SQLite on the event loop")
    monkeypatch.setattr(store, "_connect", refused)
    assert store.store_id == expected
