"""Report raw whole-path timings and store bytes, without a performance verdict."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3
import statistics
import time

import pytest
from doceditor.document_scope import MAX_TEXT_BYTES
from doceditor.live_document import LiveDocument
from doceditor.live_lane import DocumentLane, Operation
from doceditor.document_save import write_atomic_plain
from doceditor.live_runtime import LiveDocuments
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


pytestmark = pytest.mark.skipif(
    os.environ.get("CORSIA_DOCUMENT_LIVE_MEASURE") != "1",
    reason="Raw document measurements require CORSIA_DOCUMENT_LIVE_MEASURE=1",
)


class MeasuringSocket(Socket):
    def __init__(self):
        super().__init__()
        self.updates = []
        self.acks = {}

    async def send_json(self, frame):
        if frame["type"] == "doc_update":
            self.updates.append(time.perf_counter())
        elif frame["type"] == "doc_ack":
            for message_id in frame["message_ids"]:
                self.acks[message_id] = time.perf_counter()
        await super().send_json(frame)


def distribution(values):
    ordered = sorted(values)
    return {"samples": len(values), "mean_ms": statistics.mean(values) * 1000,
        "p50_ms": ordered[len(values) // 2] * 1000,
        "p99_ms": ordered[max(0, len(values) - 2)] * 1000, "worst_ms": max(values) * 1000}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [20000, MAX_TEXT_BYTES])
async def test_report_whole_receive_with_three_viewers(ownership_scope, tmp_path, monkeypatch, size):
    from doceditor import live_runtime
    from ws import document_control
    scope = ownership_scope
    _write(scope, "receive.md", "x" * size)
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
        db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    sockets, peers = [MeasuringSocket() for _ in range(3)], []
    try:
        for socket in sockets:
            await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(scope["category"] / "receive.md"),
                "project_id": "project-alpha", "thread_id": scope["owner"]})
            opened = await socket.next("doc_opened")
            peers.append(clone(base64.b64decode(opened["update"])))
        identity = {key: opened[key] for key in ("artifact_id", "store_id")}
        entry = manager.documents[opened["artifact_id"]]
        entry.live.track_range("sample", 100, 200)
        receive, delivered, durable, wire_bytes, broadcasts = [], [], [], [], []
        changed = document_control._changed
        def timed_broadcast(*args):
            started = time.perf_counter()
            changed(*args)
            broadcasts.append(time.perf_counter() - started)
        monkeypatch.setattr(document_control, "_changed", timed_broadcast)
        for index in range(100):
            before = peers[0].get_state()
            # Same-size replacement keeps MAX_TEXT_BYTES valid for all samples.
            del peers[0]["text"][0:1]
            peers[0]["text"].insert(0, "a" if index % 2 else "b")
            delta = peers[0].get_update(before)
            frame = {**identity, "type": "doc_update", "message_id": str(index), "update": base64.b64encode(delta).decode()}
            started = time.perf_counter()
            await document_control.handle_frame(sockets[0], user, frame)
            receive.append(time.perf_counter() - started)
            peer_updates = []
            for socket in sockets[1:]:
                update = await socket.next("doc_update")
                wire_bytes.append(len(update["update"]))
                peer_updates.append(update["update"])
            delivered.append(max(socket.updates[-1] for socket in sockets[1:]) - started)
            await sockets[0].next("doc_ack")
            durable.append(sockets[0].acks[str(index)] - started)
            for peer, update in zip(peers[1:], peer_updates):
                peer.apply_update(base64.b64decode(update))
            assert all(str(peer["text"]) == entry.live.text for peer in peers)
        print("RECEIVE_MEASUREMENT=" + json.dumps({"text_bytes": size, "viewers": 3, "ranges": 1,
            "handle_frame": distribution(receive),
            "scheduled_broadcast": distribution(broadcasts[:100]),
            "through_two_peer_sends": distribution(delivered),
            "through_durable_ack": distribution(durable), "base64_delta_bytes_mean": statistics.mean(wire_bytes)}, sort_keys=True))
    finally:
        for socket in sockets:
            await document_control.disconnect(socket)
        await manager.close()


def footprint(store):
    with sqlite3.connect(store.path) as db:
        return {"journal_bytes": db.execute("SELECT COALESCE(SUM(length(upd)),0) FROM live").fetchone()[0],
            "mirror_bytes": db.execute("SELECT COALESCE(SUM(length(state)),0) FROM mirror").fetchone()[0],
            "journal_rows": db.execute("SELECT COUNT(*) FROM live").fetchone()[0],
            "mirror_rows": db.execute("SELECT COUNT(*) FROM mirror").fetchone()[0], "sqlite_bytes": store.path.stat().st_size}


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_every", [1, 1000])
async def test_report_journal_and_mirror_per_thousand_typed_characters(tmp_path, flush_every):
    path = tmp_path / "growth.md"
    path.write_text("x" * 20000)
    live = await LiveDocument.open(path, tmp_path / "store.sqlite")
    async def valid(text, actor=None):
        pass
    async def write(text, prior_hash):
        await asyncio.to_thread(write_atomic_plain, path, text, prior_hash)
    lane = DocumentLane(live, validate_actor=valid, write=write)
    peer = clone(live.doc.get_update())
    initial = footprint(live.store)
    try:
        for index in range(1000):
            before = peer.get_state()
            peer["text"].insert(len(peer["text"]), "a")
            assert live.receive(BrowserMessage(str(index), peer.get_update(before)))
            if (index + 1) % flush_every == 0:
                await lane.submit(Operation("flush"))
        assert live.text == path.read_text() == "x" * 20000 + "a" * 1000
        final = footprint(live.store)
        print("STORE_MEASUREMENT=" + json.dumps({"initial_text_bytes": 20000, "typed_characters": 1000,
            "flush_every_characters": flush_every, "final_text_bytes": 21000,
            "initial": initial, "final": final, "growth": {key: final[key] - initial[key] for key in initial}}, sort_keys=True))
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_report_receive_cost_per_single_character_update_near_the_size_limit(tmp_path):
    path = tmp_path / "large.md"
    path.write_text("x" * (MAX_TEXT_BYTES - 5000))
    live = await LiveDocument.open(path, tmp_path / "store.sqlite")
    peer = clone(live.doc.get_update())
    timings = []
    try:
        # Nothing commits, so every receive also checks a growing pending backlog.
        for index in range(2000):
            before = peer.get_state()
            peer["text"].insert(index, "a")
            message = BrowserMessage(str(index), peer.get_update(before))
            started = time.perf_counter()
            assert live.receive(message)
            timings.append(time.perf_counter() - started)
        assert live.text == str(peer["text"])
        print("RECEIVE_COST=" + json.dumps({"text_bytes": len(live.text), "pending_messages": len(live.received),
            "per_update": distribution(timings)}, sort_keys=True))
    finally:
        live.close()
