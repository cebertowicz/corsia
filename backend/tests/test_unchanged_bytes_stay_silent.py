"""Saving a document with its bytes unchanged neither rewrites the file nor tells its owner of an edit.

Closing the last tab on a live document flushes it with no new text. That flush
used to rename identical bytes over the file (moving its modified time) and
publish a row whose before and after text were equal, which the owner read as
"edited by a non-owner".
"""
import asyncio
import os

import pytest

from doceditor.live_lane import Operation
from registry import service
from registry.identity_metadata import parse_metadata
from tests.test_document_live_publisher import events, owed
from tests.test_document_live_shared_lane import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write

# A modified time no write in this test can produce, so any rename shows.
_OLD_NS = 1_700_000_000_000_000_000


async def _live(scope, tmp_path, actor: str):
    """A live lane on ``live.md`` whose file writer is the server's own."""
    from doceditor.live_text_write import write_live_text

    live, lane, _policy, write_actor = await opened(scope, tmp_path, actor)

    async def writer(text, prior_hash, operation=None):
        await asyncio.to_thread(write_live_text, live.path, text, prior_hash, operation)

    lane.write = writer
    return live, lane, write_actor


def _publish_owed(scope, live) -> None:
    from doceditor.live_publisher import publish_one

    while (row := live.store.next_publication()) is not None:
        publish_one(live.store, row, db=scope["db"])


def _stamp(scope) -> dict:
    artifact = service.find_file_identity(str(scope["category"] / "live.md"), db=scope["db"])
    return parse_metadata(artifact["metadata"]).get("live_publication", {})


@pytest.mark.asyncio
async def test_a_flush_with_unchanged_text_leaves_the_file_untouched(ownership_scope, tmp_path):
    scope = ownership_scope
    await asyncio.to_thread(_write, scope, "live.md", "body\n")
    path = scope["category"] / "live.md"
    os.utime(path, ns=(_OLD_NS, _OLD_NS))
    live, lane, actor = await _live(scope, tmp_path, "conductor")
    try:
        assert (await lane.submit(Operation("flush", actor=actor))).server_saved
        assert path.read_bytes() == b"body\n"
        assert path.stat().st_mtime_ns == _OLD_NS
        # The flush is still recorded for publication: its stamp and comment positions land in the database.
        assert owed(live.store)
        await asyncio.to_thread(_publish_owed, scope, live)
        assert _stamp(scope).get("store_id") == live.store.store_id
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_a_non_owner_flush_with_unchanged_text_sends_no_digest_and_a_real_change_still_does(
    ownership_scope, tmp_path,
):
    scope = ownership_scope
    await asyncio.to_thread(_write, scope, "live.md", "body\n")
    path = scope["category"] / "live.md"
    live, lane, actor = await _live(scope, tmp_path, "conductor")
    try:
        assert (await lane.submit(Operation("flush", actor=actor))).server_saved
        await asyncio.to_thread(_publish_owed, scope, live)
        assert events(scope) == []
        assert (await lane.submit(Operation("replace", content="changed\n", actor=actor))).server_saved
        assert path.read_bytes() == b"changed\n"
        await asyncio.to_thread(_publish_owed, scope, live)
        assert len(events(scope)) == 1
    finally:
        await lane.close()
        live.close()


def test_a_file_tool_write_of_identical_bytes_sends_no_digest_and_a_real_change_still_does(ownership_scope):
    scope = ownership_scope
    _write(scope, "live.md", "body\n")
    _write(scope, "live.md", "body\n", actor=scope["conductor"])
    assert events(scope) == []
    _write(scope, "live.md", "changed\n", actor=scope["conductor"])
    assert len(events(scope)) == 1
