"""Stale install targets must short-circuit before any cold history validation."""
from dataclasses import replace

import pytest

from test_checkpoint_store import _capture, _db, _request, _seed_thread


@pytest.mark.parametrize("invalid", ["thread-gone", "epoch-moved", "boundary-missing"])
def test_cold_validation_never_runs_for_ineligible_target(monkeypatch, invalid):
    from infra import checkpoints

    db = _db()
    _seed_thread(db, "thread")
    record = checkpoints.create_running(replace(_request("thread", "cold"), mode="cold"), db=db)
    _capture(db, record.id)
    # Model a retained generation left behind by deletion, as legacy rows can be.
    if invalid == "thread-gone":
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM threads WHERE id='thread'")
    elif invalid == "epoch-moved":
        db.execute("UPDATE threads SET history_epoch=1 WHERE id='thread'")
    else:
        db.execute("DELETE FROM messages WHERE id='thread-m3'")
    db.commit()

    def forbidden(*_args):
        raise AssertionError("ineligible target reached cold validation")

    monkeypatch.setattr(checkpoints, "cold_install_refusal", forbidden)
    try:
        result = checkpoints.install(record.id, installed_at="t3", db=db)
        assert result.status == "overtaken"
        assert result.error == "checkpoint boundary was overtaken"
    finally:
        db.close()
