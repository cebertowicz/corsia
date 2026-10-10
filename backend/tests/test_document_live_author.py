"""The doc_update broadcast names the writing agent only for an agent live write."""
import pytest

from doceditor.live_document import file_hash
from doceditor.live_lane import Operation
from doceditor.live_operation import TextEdit
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from infra.database import get_db
from tests.registry_helpers import make_session
from tests.test_document_live_lane import open_lane
from ws import document_control

pytestmark = pytest.mark.usefixtures("isolated_db")


def announcements(live):
    seen = []
    live.update_changed = lambda update, sender, author=None: seen.append((sender, author))
    return seen


@pytest.mark.asyncio
async def test_agent_edit_is_announced_with_its_thread_title(tmp_path):
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha")
    live, lane = await open_lane(tmp_path)
    seen = announcements(live)
    await lane.submit(Operation("edit", edits=(TextEdit("quote", "quote more"),), actor=WriteActor(thread_id, None)))
    assert seen == [(None, {"thread_id": thread_id, "name": "Agent Alpha"})]


@pytest.mark.asyncio
async def test_untitled_agent_is_named_by_its_thread_id(tmp_path):
    db = get_db()
    thread_id = make_session(db, title="")
    live, lane = await open_lane(tmp_path)
    seen = announcements(live)
    await lane.submit(Operation("write", content="hello again", actor=WriteActor(thread_id, None),
                                base_hash=file_hash(live.path.read_bytes())))
    assert seen[-1][1] == {"thread_id": thread_id, "name": thread_id}


@pytest.mark.asyncio
async def test_failed_author_lookup_still_lands_the_write(tmp_path, monkeypatch):
    from doceditor import live_author

    def broken(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(live_author, "get_thread", broken)
    db = get_db()
    thread_id = make_session(db, title="Agent Alpha")
    live, lane = await open_lane(tmp_path)
    seen = announcements(live)
    result = await lane.submit(Operation("edit", edits=(TextEdit("quote", "quote more"),),
                                         actor=WriteActor(thread_id, None)))
    assert result.server_saved
    assert "quote more" in live.path.read_text()
    assert seen == [(None, {"thread_id": thread_id, "name": thread_id})]


@pytest.mark.asyncio
async def test_unsaved_update_is_never_credited_to_the_next_writer(tmp_path):
    live, _lane = await open_lane(tmp_path)
    seen = announcements(live)
    unsaved = clone(live.doc.get_update())
    state = unsaved.get_state()
    unsaved["text"].insert(0, "unsaved ")
    live.apply_unsaved(unsaved.get_update(state))
    agent = clone(live.doc.get_update())
    state = agent.get_state()
    agent["text"].insert(0, "agent ")
    alpha = {"thread_id": "t", "name": "Alpha"}
    await live.apply_committed(agent.get_update(state), agent.get_state(), "hash", alpha)
    assert seen == [(None, None), (None, alpha)]


@pytest.mark.asyncio
async def test_user_write_and_browser_sync_carry_no_author(tmp_path):
    live, lane = await open_lane(tmp_path)
    seen = announcements(live)
    await lane.submit(Operation("edit", edits=(TextEdit("hello", "hi"),),
                                actor=WriteActor("ui", None, direct_user=True, user_id="u1")))
    peer = clone(live.doc.get_update())
    state = peer.get_state()
    peer["text"].insert(0, "typed ")
    live.receive(BrowserMessage("typing", peer.get_update(state)), sender="socket")
    assert [author for _, author in seen] == [None, None]


def test_doc_update_frame_includes_author_only_when_given(monkeypatch):
    frames = []
    monkeypatch.setattr(document_control, "_broadcast", lambda artifact_id, frame, *, exclude=None: frames.append(frame))

    class Store:
        store_id = "store"

    class Live:
        store = Store()
        digest = "d"
        ranges: dict = {}

    class Entry:
        live = Live()

    document_control._changed("artifact", Entry(), b"u", None)
    document_control._changed("artifact", Entry(), b"u", None, {"thread_id": "t", "name": "Alpha"})
    assert "author" not in frames[0]
    assert frames[1]["author"] == {"thread_id": "t", "name": "Alpha"}
