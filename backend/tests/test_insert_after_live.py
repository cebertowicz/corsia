"""Insertions in the live document use the same suggestion decision path."""
import pytest

from doceditor import comments, comment_actions
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import legacy_writer, user_id
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_live_insertions_keep_heading_and_creation_order(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    artifact = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(entry.live.path),)).fetchone()[0]
    rows = []
    for text in (" one", " two"):
        row, _ = comments.create_with_status(artifact, body="", author="author", author_kind="agent",
            author_thread_id=scope["owner"], anchor_text="b", from_off=2, to_off=3,
            replacement=text, insert_after=True, db=scope["db"])
        entry.live.track_range(row["id"], 2, 3)
        rows.append(row)
    try:
        for row in rows:
            await comment_actions.decide_suggestion(row["id"], "accept", user_id=user_id(scope),
                decided_by="user", actor_thread_id=None, write=legacy_writer)
        assert entry.live.text == "a b one two\nsecond\n"
        assert entry.live.path.read_text() == entry.live.text
    finally:
        await manager.close()
