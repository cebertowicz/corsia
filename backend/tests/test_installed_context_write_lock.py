"""Installed-context writes keep one canonical boundary resolver (#2755)."""

from __future__ import annotations

from contextlib import contextmanager

from test_installed_context_parity import (
    THREAD,
    _collect,
    _db,
    _dispatch,
    _install_checkpoint,
    _prepared,
    parity,
)


def _metadata(message_id: str) -> str:
    db = _db()
    try:
        return db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND id=?",
            (THREAD, message_id),
        ).fetchone()["metadata"]
    finally:
        db.close()


def test_missing_checkpoint_receipt_falls_back_to_first_message(parity):
    from core.installed_context import (
        INSTALLED_SOURCES,
        persist_prepared_installed_context,
    )

    model = "anthropic/claude-test"
    _install_checkpoint("cp-1", model)
    db = _db()
    try:
        db.execute("DELETE FROM messages WHERE id='checkpoint-receipt:cp-1'")
        db.commit()
    finally:
        db.close()

    collection = _collect(model)
    assert collection.installed.boundary_id == "m1"
    assert persist_prepared_installed_context(
        THREAD, _prepared("native", collection),
    )
    assert INSTALLED_SOURCES in _metadata("m1")


def test_boundary_change_after_prepare_skips_write_and_cleanup(
    parity, monkeypatch, caplog,
):
    from core.installed_context import (
        INSTALLED_SOURCES,
        persist_prepared_installed_context,
    )
    from infra import database

    model = "anthropic/claude-test"
    _dispatch("native", model)
    _install_checkpoint("cp-1", model)
    collection = _collect(model)
    original_write_transaction = database.write_transaction

    @contextmanager
    def install_new_boundary_before_lock(db, *, scope):
        _install_checkpoint("cp-2", model)
        with original_write_transaction(db, scope=scope) as connection:
            yield connection

    monkeypatch.setattr(database, "write_transaction", install_new_boundary_before_lock)
    caplog.set_level("INFO", logger="core.installed_context")

    assert not persist_prepared_installed_context(THREAD, _prepared("native", collection))
    assert "written=False reason=boundary-moved" in caplog.text
    assert INSTALLED_SOURCES in _metadata("m1")
