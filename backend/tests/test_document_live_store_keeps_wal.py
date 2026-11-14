"""A refused store keeps its main file and -wal as found (design 5.7a).

A crash image is copied while a writer still holds the store open, so its -wal
holds committed frames that were never checkpointed into the main file.
"""
import gc
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
from contextlib import closing

import pytest

from doceditor.live_admission import store_refusal
from doceditor.live_store import LiveStore, StoreRefused

ACKED = b"acknowledged edit"


def _wal(path):
    return path.with_name(path.name + "-wal")


def _wal_pages(path):
    """Page numbers of the frames in a -wal (32-byte header, 24-byte frame headers)."""
    data = _wal(path).read_bytes()
    page_size = struct.unpack(">I", data[8:12])[0]
    frame = 24 + page_size
    return [struct.unpack(">I", data[32 + i * frame:36 + i * frame])[0] for i in range((len(data) - 32) // frame)]


def _crash_image(tmp_path, change):
    """A healthy store, then ``change`` committed only to the -wal, copied as a crash would leave it."""
    source = tmp_path / "source.sqlite"
    LiveStore(source).initialize(b"initial", "hash")
    writer = sqlite3.connect(source, isolation_level=None)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        change(writer)
        image = tmp_path / "image.sqlite"
        shutil.copyfile(source, image)
        shutil.copyfile(_wal(source), _wal(image))
    finally:
        writer.close()
    return image


def _damage_page_one(path):
    data = bytearray(path.read_bytes())
    data[0:16] = b"not a database!!"
    path.write_bytes(bytes(data))


def _row(writer):
    writer.execute("INSERT INTO live(upd) VALUES (?)", (ACKED,))


def test_refused_damaged_store_keeps_main_and_wal_bytes(tmp_path):
    image = _crash_image(tmp_path, _row)
    assert 1 not in _wal_pages(image)
    _damage_page_one(image)
    main, wal = image.read_bytes(), _wal(image).read_bytes()
    assert ACKED in wal and ACKED not in main
    with pytest.raises(StoreRefused) as refused:
        LiveStore(image)
    assert store_refusal(refused.value).kind == "store_damaged"
    del refused
    gc.collect()
    assert image.read_bytes() == main
    assert _wal(image).read_bytes() == wal


def test_wal_holding_page_one_opens_and_reads_the_edit(tmp_path):
    def change(writer):
        writer.execute("CREATE TABLE extra(x)")
        _row(writer)
    image = _crash_image(tmp_path, change)
    assert 1 in _wal_pages(image)
    _damage_page_one(image)
    assert ACKED in LiveStore(image).load_updates()


_UNCOMMITTED_WRITER = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size=1")
db.execute("BEGIN")
for _ in range(64):
    db.execute("INSERT INTO live(upd) VALUES (?)", (os.urandom(4000),))
os._exit(0)
"""


def test_rollback_store_with_hot_journal_rolls_back_and_opens(tmp_path):
    path = tmp_path / "rollback.sqlite"
    LiveStore(path).initialize(b"initial", "hash")
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    # A writer spills an uncommitted transaction into the main file, then dies.
    subprocess.run([sys.executable, "-c", _UNCOMMITTED_WRITER, str(path)], check=True)
    journal = path.with_name(path.name + "-journal")
    assert journal.stat().st_size > 0
    assert LiveStore(path).load_updates() == (b"initial",)
    assert not journal.exists()


def test_healthy_store_still_checkpoints_and_removes_its_wal(tmp_path):
    path = tmp_path / "healthy.sqlite"
    LiveStore(path).initialize(b"initial", "hash")
    store = LiveStore(path)
    assert store.load_updates() == (b"initial",)
    assert sorted(os.listdir(tmp_path)) == ["healthy.sqlite"]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connection_refused_while_connecting_is_closed_at_once(tmp_path, monkeypatch):
    closed = []

    class Tracked(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    path = tmp_path / "damaged.sqlite"
    path.write_bytes(b"not a database " * 64)
    store = LiveStore.__new__(LiveStore)
    store.path = path
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: real(*args, factory=Tracked, **kwargs))
    with pytest.raises(sqlite3.DatabaseError):
        store._connect()
    assert len(closed) == 1
