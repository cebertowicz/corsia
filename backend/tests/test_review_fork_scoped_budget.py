"""Review-fork admission counts only the checkpoint-scoped render source."""

from __future__ import annotations

import asyncio

import pytest

from core.artifact_review_capture import ReviewForkFailure
from history.types import HistoryBoundary
from test_doc_review_requests import request, review_env, stored, text_document
from test_review_fork_cold_request import TARGET


_LEGACY_META = '{"turn_unbound_reason":"legacy_import"}'


def _append_legacy_rows(db, start: int, count: int) -> None:
    db.executemany(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'reviewer','user',?,'{\"turn_unbound_reason\":\"legacy_import\"}','t0',?)",
        [(f"history-{seq}", f"history {seq}", seq) for seq in range(start, start + count)],
    )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id='reviewer'", (start + count,))
    db.commit()


def _append_legacy_segment(db, start: int) -> int:
    """Append a two-row legacy segment and return its assistant anchor sequence."""
    db.executemany(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES "
        "(?,'reviewer',?,?,?,'t0',?)",
        [
            (f"segment-{start}", "user", "segment user", _LEGACY_META, start),
            (f"segment-{start + 1}", "assistant", "segment assistant", _LEGACY_META, start + 1),
        ],
    )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id='reviewer'", (start + 2,))
    db.commit()
    return start + 1


def _install_checkpoint(db, through_seq: int, through_message_id: str | None = None):
    from core.checkpoint_types import CheckpointRequest
    from infra.checkpoints import capture, create_running, install

    checkpoint = create_running(CheckpointRequest(
        id=f"checkpoint-{through_seq}",
        thread_id="reviewer",
        history_epoch=0,
        previous_generation_id=None,
        through_seq=through_seq,
        through_message_id=through_message_id or f"history-{through_seq}",
        trigger="manual",
        mode="warm",
        source_harness="claude-code",
        source_model="claude-code/fixture-cc",
        writer_harness="claude-code",
        writer_model="claude-code/fixture-cc",
        originating_turn_id=None,
        first_included_seq=1,
        started_at="t1",
    ), db=db)
    capture(checkpoint.id, raw_result="checkpoint body", usage_json=None, captured_at="t2", db=db)
    installed = install(checkpoint.id, installed_at="t3", db=db)
    db.commit()
    return installed


def _prepare_cold_target(monkeypatch) -> None:
    monkeypatch.setattr("llm.resolution.resolve_thread_target", lambda *_args, **_kwargs: TARGET)
    monkeypatch.setattr("core.fork_render.fork_binary", lambda *_args, **_kwargs: "claude")


@pytest.mark.asyncio
async def test_checkpointed_long_history_starts_disposable_review_pass(review_env, monkeypatch, tmp_path):
    """A short scoped tail still starts even when covered history exceeds the cap."""
    from core import artifact_review_runtime as runtime
    from core.fork_runtime import handles

    db, artifact_id = review_env
    text_document(db, artifact_id, tmp_path)
    _append_legacy_rows(db, 1, 1023)
    anchor_seq = _append_legacy_segment(db, 1024)
    _append_legacy_rows(db, 1026, 1)
    _install_checkpoint(db, anchor_seq, f"segment-{anchor_seq}")
    db.execute(
        "INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('busy','reviewer',0,'test-model','running','t4','boot')"
    )
    db.commit()
    row = request(db, artifact_id)
    _prepare_cold_target(monkeypatch)
    entered = asyncio.Event()

    async def run(_request_id, _cold_request, cancelled):
        entered.set()
        await cancelled.wait()

    monkeypatch.setattr(runtime, "_run", run)
    await runtime.start(row["id"])
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        assert row["fork_id"] in handles
        assert stored(db, row["id"])["state"] == "forked"
        assert stored(db, row["id"])["node_id"] is None
    finally:
        handle = handles.pop(row["fork_id"])
        handle.cancel_event.set()
        await handle.task


@pytest.mark.asyncio
async def test_scoped_overflow_hands_review_to_continuation_without_launch(review_env, monkeypatch):
    from core import artifact_review_runtime as runtime
    from core.fork_runtime import handles
    from plans import public

    db, artifact_id = review_env
    _append_legacy_rows(db, 1, 1023)
    anchor_seq = _append_legacy_segment(db, 1024)
    _append_legacy_rows(db, 1026, 1022)
    _install_checkpoint(db, anchor_seq, f"segment-{anchor_seq}")
    row = request(db, artifact_id)
    _prepare_cold_target(monkeypatch)
    launched: list[str] = []

    async def run(request_id, _cold_request, _cancelled):
        launched.append(request_id)

    monkeypatch.setattr(runtime, "_run", run)
    await runtime.start(row["id"])
    await asyncio.sleep(0)

    result = stored(db, row["id"])
    assert (result["state"], result["reason"]) == ("failed", "history_tail_bound")
    assert result["node_id"] and public.review_is_open(db, result["node_id"])
    assert row["fork_id"] not in handles
    assert launched == []


@pytest.mark.parametrize(("tail_rows", "raises"), [(1021, False), (1022, True)])
def test_checkpoint_scoped_budget_boundary_and_overflow_has_no_payload_read(
    review_env, monkeypatch, tail_rows, raises,
):
    from core import artifact_review_source as source

    db, _artifact_id = review_env
    _append_legacy_rows(db, 1, 1023)
    anchor_seq = _append_legacy_segment(db, 1024)
    _append_legacy_rows(db, 1026, tail_rows)
    previous = _install_checkpoint(db, anchor_seq, f"segment-{anchor_seq}")
    from history.scoped_source import segment_start

    assert segment_start(db, "reviewer", HistoryBoundary.of_checkpoint(previous)).first_seq == 1024
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='reviewer' AND delivery_state='delivered' AND seq >= 1024"
    ).fetchone()[0] == 3 + tail_rows
    selects: list[str] = []
    db.set_trace_callback(lambda statement: selects.append(statement) if statement.lstrip().upper().startswith("SELECT") else None)
    try:
        if raises:
            monkeypatch.setattr(source, "render_portable_history", pytest.fail)
            with pytest.raises(ReviewForkFailure, match="history_tail_bound"):
                source.bounded_review_history("reviewer", previous, db=db)
        else:
            projection = source.bounded_review_history("reviewer", previous, db=db)
            assert projection.sealed_boundary is not None
    finally:
        db.set_trace_callback(None)
    assert selects
    if raises:
        assert all("content" not in statement.lower() for statement in selects)


def test_scoped_payload_reads_skip_nonsemantic_gap_before_legacy_segment(review_env):
    from history.scoped_source import scoped_source

    db, _artifact_id = review_env
    _append_legacy_rows(db, 1, 1)
    db.executemany(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'reviewer','system','covered payload','{}','t0',?)",
        [(f"covered-{seq}", seq) for seq in range(2, 1027)],
    )
    anchor_seq = _append_legacy_segment(db, 1027)
    factory, payload_seqs = db.row_factory, []

    def count_payload_rows(cursor, values):
        row = factory(cursor, values)
        if "content" in row.keys():
            payload_seqs.append(row["seq"])
        return row

    db.row_factory = count_payload_rows
    try:
        source = scoped_source(db, "reviewer", HistoryBoundary("reviewer", 0, anchor_seq, f"segment-{anchor_seq}"))
    finally:
        db.row_factory = factory
    assert [row["seq"] for row in source.rows] == [1027, 1028]
    assert source.predecessor["seq"] == 1
    assert not source.starts_bound_turn
    assert payload_seqs == [1, 1027, 1028]
