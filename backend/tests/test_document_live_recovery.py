"""Held evidence survives every refused or interrupted choice."""
import sqlite3

import pytest

from doceditor.live_document import LiveDocument
from doceditor.live_store import StoreRefused
from tests.test_document_live_lane import insert, open_lane, cut_at


class Interrupted(RuntimeError):
    pass


async def held(tmp_path, monkeypatch):
    from doceditor.live_lane import Operation

    async def cut(stage):
        if stage == "record":
            raise Interrupted("record cut")

    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    insert(live, "typing")
    with pytest.raises(Interrupted):
        await lane.submit(Operation("replace", content="AGENT"))
    live.path.write_text("OUTSIDE")
    cut_at(monkeypatch, None)
    await lane.recover()
    assert live.store.pending().state == "held"
    return live, lane


@pytest.mark.asyncio
async def test_held_never_discarded_when_file_returns_to_prior(tmp_path, monkeypatch):
    live, lane = await held(tmp_path, monkeypatch)
    record = live.store.pending()
    live.path.write_text("hello quote")
    await lane.recover()
    assert live.store.pending() == record
    assert live.text == "USER hello quote"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, expected", [("corsia", "USER hello quote"), ("file", "OUTSIDE")])
async def test_choice_commits_received_closure_and_retires_held(tmp_path, kind, expected, monkeypatch):
    live, lane = await held(tmp_path, monkeypatch)
    acks = []
    live.acknowledge = acks.append
    insert(live, "during", "LATE ")
    result = await lane.choose(kind)
    expected = "LATE " + expected if kind == "corsia" else expected
    assert result.server_saved
    assert live.text == live.path.read_text() == expected
    assert acks == [("during",)]
    assert live.store.pending() is None
    assert (await LiveDocument.open(live.path, live.store.path)).text == expected


@pytest.mark.asyncio
async def test_refused_choice_transaction_keeps_both_texts_and_messages(tmp_path, monkeypatch):
    live, lane = await held(tmp_path, monkeypatch)
    original = live.store.pending()
    insert(live, "during", "LATE ")
    db = sqlite3.connect(live.store.path, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StoreRefused):
            await lane.choose("corsia")
    finally:
        db.execute("ROLLBACK")
        db.close()
    assert live.store.pending() == original
    assert live.text == "LATE USER hello quote"
    assert live.path.read_text() == "OUTSIDE"
    assert list(live.received) == ["during"]


@pytest.mark.asyncio
async def test_choice_cut_before_rename_clears_choice_not_held_evidence(tmp_path, monkeypatch):
    live, lane = await held(tmp_path, monkeypatch)
    original = live.store.pending()
    insert(live, "during", "LATE ")

    async def cut(stage):
        if stage == "choice_record":
            raise Interrupted("choice record cut")

    cut_at(monkeypatch, cut)
    with pytest.raises(Interrupted):
        await lane.choose("corsia")
    assert live.store.pending().choice is not None
    live, lane = await open_lane(tmp_path)
    await lane.recover()
    assert live.store.pending() == original
    assert live.path.read_text() == "OUTSIDE"
    assert live.text == "LATE USER hello quote"


@pytest.mark.asyncio
async def test_choice_cut_after_rename_applies_stored_items_once(tmp_path, monkeypatch):
    live, lane = await held(tmp_path, monkeypatch)
    insert(live, "during", "LATE ")

    async def cut(stage):
        if stage == "choice_rename":
            raise Interrupted("choice rename cut")

    cut_at(monkeypatch, cut)
    with pytest.raises(Interrupted):
        await lane.choose("corsia")
    live, lane = await open_lane(tmp_path)
    await lane.recover()
    assert live.store.pending() is None
    assert live.path.read_text() == live.text == "LATE USER hello quote"
    await lane.recover()
    assert live.text.count("LATE") == 1


@pytest.mark.asyncio
async def test_refused_rename_and_actual_refused_clear_block_new_choices(tmp_path, monkeypatch):
    from doceditor.live_lane import LaneRefused, Operation

    live, lane = await held(tmp_path, monkeypatch)
    original = live.store.pending()
    db = None

    async def refused(text, prior_hash):
        nonlocal db
        db = sqlite3.connect(live.store.path, isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        raise PermissionError("rename refused")

    lane.write = refused
    try:
        with pytest.raises(StoreRefused):
            await lane.choose("corsia")
        record = live.store.pending()
        assert record.choice is not None
        assert (record.candidate, record.mirror_state, record.prior_hash, record.result_hash) == (
            original.candidate, original.mirror_state, original.prior_hash, original.result_hash,
        )
        with pytest.raises(LaneRefused):
            await lane.choose("file")
        with pytest.raises(LaneRefused):
            refused = await lane.submit(Operation("flush"))
            assert refused.outcome == "refused"
            refused.require_landed()
    finally:
        db.execute("ROLLBACK")
        db.close()
    live, lane = await open_lane(tmp_path)
    await lane.recover()
    assert live.store.pending() == original
    assert live.path.read_text() == "OUTSIDE"
    assert live.text == "USER hello quote"
