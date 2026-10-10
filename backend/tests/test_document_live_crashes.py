"""Kill real processes at file/store cuts, then load the same item identities."""
import asyncio
import os
from pathlib import Path
import sys
import threading

import pytest

from doceditor.live_document import file_hash
from doceditor.live_lane import Operation
from tests.test_document_live_lane import insert, open_lane


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["record", "rename", "promote"])
@pytest.mark.parametrize("edit", ["insert", "delete"])
async def test_killed_lane_process_converges_once(tmp_path, cut, edit):
    await killed(tmp_path, cut, edit)
    live, lane = await open_lane(tmp_path)
    await lane.recover()
    await lane.submit(Operation("flush"))
    expected = "USER hello quote" if edit == "insert" else "quote"
    if cut != "record":
        expected = "USER hello world" if edit == "insert" else "world"
    assert live.path.read_text() == live.text == expected
    assert live.store.pending() is None
    await lane.recover()
    assert live.text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["choice_record", "choice_rename", "choice_apply", "choice_clear"])
@pytest.mark.parametrize("edit", ["insert", "delete"])
async def test_killed_choice_process_preserves_held_or_applies_once(tmp_path, cut, edit):
    await killed(tmp_path, cut, edit)
    live, lane = await open_lane(tmp_path)
    record = live.store.pending()
    original = (record.candidate, record.mirror_state, record.prior_hash, record.result_hash)
    await lane.recover()
    expected = "LATE USER hello quote" if edit == "insert" else "USER quote"
    if cut in ("choice_record", "choice_clear"):
        recovered = live.store.pending()
        assert recovered.state == "held" and recovered.choice is None
        assert (recovered.candidate, recovered.mirror_state, recovered.prior_hash,
                recovered.result_hash) == original
        assert live.path.read_text() == "OUTSIDE"
    else:
        assert live.store.pending() is None
        assert live.path.read_text() == expected
    assert live.text == expected
    assert not live.path.with_name(f".corsia-live-{live.path.name}.stage").exists()
    await lane.recover()
    assert live.text == expected


async def killed(tmp_path, cut, edit):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), str(tmp_path), cut, edit,
        cwd=tmp_path, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        # A crashed child ends the line at EOF; this bound only stops a hang (cold import can exceed 10 s).
        line = await asyncio.wait_for(process.stdout.readline(), 120)
        if line != b"CUT\n":
            _, error = await process.communicate()
            pytest.fail(f"Child did not reach {cut}: {error.decode()}")
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode != 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def park_transaction(store, method_name):
    original_method = getattr(store, method_name)
    original_transaction = store._transaction

    def transaction(action):
        def interrupted(db):
            result = action(db)
            print("CUT", flush=True)
            threading.Event().wait()
            return result
        return original_transaction(interrupted)

    def method(*args):
        store._transaction = transaction
        return original_method(*args)

    setattr(store, method_name, method)


async def worker(root, cut, edit):
    # This runs in its own subprocess, so the stage hook is set on the module directly.
    from doceditor import live_lane

    async def no_cut(stage):
        return None

    async def checkpoint(stage):
        if stage == cut and stage not in ("promote", "choice_apply"):
            print("CUT", flush=True)
            await asyncio.Event().wait()

    live, lane = await open_lane(root)
    base = file_hash(live.path.read_bytes())
    insert(live, "typing")
    if cut.startswith("choice_"):
        async def interrupted(stage):
            if stage == "record":
                raise RuntimeError("before rename")
        live_lane._checkpoint = interrupted
        try:
            await lane.submit(Operation("replace", content="AGENT"))
        except RuntimeError:
            pass
        live.path.write_text("OUTSIDE")
        live_lane._checkpoint = no_cut
        await lane.recover()
        if edit == "insert":
            insert(live, "late", "LATE ")
        else:
            delete(live, 5, 11, "late")
        live_lane._checkpoint = checkpoint
        if cut == "choice_apply":
            park_transaction(live.store, "apply_choice")
        if cut == "choice_clear":
            def clearing(_):
                print("CUT", flush=True)
                threading.Event().wait()
            live.store.clear_choice = clearing

            async def refused(text, prior_hash):
                stage = live.path.with_name(f".corsia-live-{live.path.name}.stage")
                stage.write_text(text)
                raise PermissionError("rename refused")
            lane.write = refused
        await lane.choose("corsia")
    else:
        if edit == "delete":
            # Drop USER plus hello, retaining only the original quote's items.
            delete(live, 0, 11, "deletion")
        live_lane._checkpoint = checkpoint
        if cut == "promote":
            park_transaction(live.store, "promote")
        await lane.submit(Operation("write", content="hello world", base_hash=base))


def delete(live, start, end, identifier):
    from doceditor.live_store import BrowserMessage
    from doceditor.live_text import clone

    peer = clone(live.doc.get_update())
    state = peer.get_state()
    del peer["text"][start:end]
    live.receive(BrowserMessage(identifier, peer.get_update(state)))


if __name__ == "__main__":
    asyncio.run(worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
