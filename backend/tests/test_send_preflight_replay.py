"""Canonical send replay: one snapshot, no deferred covered-archive fallback."""

from types import SimpleNamespace

import pytest

from tests.test_checkpoint_preflight_clean import _Result
from tests.test_history_slice_reads import LARGE, _seed, _target


def _db():
    from infra.database import get_db

    return get_db()


@pytest.mark.parametrize("installed", [False, True])
def test_candidates_freeze_one_canonical_source(isolated_db, installed):
    from core.checkpoint_resume import installed_replay
    from core.context_replay import exact_replay
    from history.projection import render_portable_history
    from infra.checkpoint_queries import current_installed
    from infra.send_replay_reads import read_candidates

    _seed(LARGE, _target("codex").catalog_id, "codex")
    db = _db()
    if not installed:
        db.execute("DELETE FROM checkpoints WHERE thread_id=?", (LARGE,))
        db.commit()
    expected_exact = exact_replay(render_portable_history(LARGE, db=db))
    expected_tail = installed_replay(current_installed(LARGE)) if installed else None
    db.close()
    candidates = read_candidates(LARGE)
    assert (candidates.history_epoch, candidates.sealed_boundary) == (
        expected_exact.history_epoch, expected_exact.sealed_boundary,
    )
    assert candidates.checkpoint_tail == expected_tail
    assert candidates.exact == (None if installed else expected_exact)


@pytest.mark.asyncio
async def test_render_uses_frozen_tail_despite_later_history(isolated_db):
    from infra.send_preflight import _read_candidates, _render_reconstruction

    _seed(LARGE, _target("codex").catalog_id, "codex")
    candidates = _read_candidates(LARGE)
    db = _db()
    db.execute("UPDATE messages SET content='later edit' WHERE thread_id=? AND role='tool'",
               (LARGE,))
    db.commit()
    db.close()
    rendered = []

    async def cold(selection, *, trigger_reason):
        rendered.append(selection)
        return "frozen-result"

    result, pinned = await _render_reconstruction(SimpleNamespace(cold=cold), candidates)
    assert result == "frozen-result"
    assert pinned is candidates
    assert rendered == [candidates.checkpoint_tail]
    assert "later edit" not in str(rendered)


def test_new_read_replaces_obsolete_checkpoint_with_current_epoch(isolated_db):
    from infra.send_replay_reads import read_candidates

    _seed(LARGE, _target("codex").catalog_id, "codex")
    previous = read_candidates(LARGE)
    db = _db()
    db.execute("UPDATE threads SET history_epoch=1 WHERE id=?", (LARGE,))
    db.commit()
    db.close()
    current = read_candidates(LARGE)
    assert previous.checkpoint_tail is not None
    assert current.history_epoch == current.exact.history_epoch == 1
    assert current.checkpoint_tail is None


def test_replay_read_keeps_one_snapshot_across_epoch_writer(isolated_db, monkeypatch):
    import sqlite3
    from infra import checkpoint_queries
    from infra.send_replay_reads import read_candidates

    _seed(LARGE, _target("codex").catalog_id, "codex")
    original = checkpoint_queries.current_installed
    changed = False

    def installed_then_change(thread_id, *, db):
        nonlocal changed
        record = original(thread_id, db=db)
        if not changed:
            changed = True
            database_path = db.execute("PRAGMA database_list").fetchone()[2]
            writer = sqlite3.connect(database_path)
            try:
                writer.execute("UPDATE threads SET history_epoch=1 WHERE id=?", (thread_id,))
                writer.commit()
            finally:
                writer.close()
        return record

    monkeypatch.setattr(checkpoint_queries, "current_installed", installed_then_change)
    candidates = read_candidates(LARGE)
    assert changed
    assert candidates.history_epoch == candidates.checkpoint_tail.history_epoch == 0
    assert read_candidates(LARGE).history_epoch == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["allowed", "blocked"])
async def test_cold_send_never_reads_covered_archive(isolated_db, monkeypatch, status):
    """A blocked checkpoint tail must reach the Turn wall, never archive replay."""
    from history import projection
    from infra import send_preflight

    target = _target("codex")
    _seed(LARGE, target.catalog_id, "codex")
    selected = []

    class Attempt:
        async def cold(self, selection, *, trigger_reason):
            selected.append(selection)
            return _Result(status, selection.mode, 20_000)

    original = projection.render_portable_history

    def scoped_only(*args, **kwargs):
        assert kwargs.get("start_exclusive") is not None, "covered archive read"
        return original(*args, **kwargs)

    monkeypatch.setattr(send_preflight, "_destination_attempt", lambda **_kw: Attempt())
    monkeypatch.setattr(projection, "render_portable_history", scoped_only)
    result = await send_preflight.context_preflight(
        thread_id=LARGE, thread_row={"model": target.catalog_id},
        provider="codex", target=target, runtime_truth={}, current_user_content="continue",
    )
    assert result["status"] == status
    assert result["continuity_mode"] == "checkpoint_tail"
    assert len(selected) == 1
    assert selected[0].tail.start_exclusive == selected[0].checkpoint_boundary


def test_invalid_installed_tail_does_not_fall_back_to_archive(isolated_db):
    from core.thread_lifecycle import TOOL_TRIM_MARKER
    from history.types import PortableHistoryUnavailableError
    from infra.send_replay_reads import read_candidates

    _seed(LARGE, _target("codex").catalog_id, "codex")
    db = _db()
    db.execute("UPDATE messages SET content=? WHERE id=?",
               (TOOL_TRIM_MARKER, f"{LARGE}-live-step-1-result-0"))
    db.commit()
    db.close()
    with pytest.raises(PortableHistoryUnavailableError):
        read_candidates(LARGE)
