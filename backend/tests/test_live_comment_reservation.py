"""A comment capture reserves its selection under the comment's own client id; the POST consumes it."""
import asyncio
import base64

import httpx
import pytest
from fastapi import FastAPI

from api import doc_comments
from auth_fixtures import OWNER_TOKEN
from doceditor import comments
from doceditor.live_document import RESERVATION_LIMIT
from doceditor.live_text import state_clocks
from doceditor.live_publication_record import WriteActor
from infra import app_loop
from infra.db_async import run_in_db
from tests.test_document_live_suggestions import typing
from tests.test_document_live_transport import Socket
from tests.test_live_comment_creation import hold, opened, snapshot
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_control


class Session:
    """One subscribed socket on the opened document, speaking the capture frames."""

    def __init__(self, scope, entry, socket=None):
        self.entry, self.socket = entry, socket or Socket()
        self.user_id = scope['db'].execute('SELECT user_id FROM threads WHERE id=?', (scope['owner'],)).fetchone()[0]
        actor = WriteActor(scope['owner'], 'project-alpha', direct_user=True)
        document_control._subscriptions[self.socket] = {
            entry.artifact_id: document_control.Subscription(entry, actor, True, self.user_id)}

    async def reserve(self, identifier, start, end, **changes):
        proof = {**snapshot(self.entry), **changes}
        await document_control.handle_frame(self.socket, {'id': self.user_id}, dict(
            type='doc_positions', artifact_id=self.entry.artifact_id, store_id=self.entry.live.store.store_id, seq=1,
            reserve=dict(client_id=identifier, from_off=start, to_off=end, vector=proof['vector'],
                         digest=proof['digest'], text_length_utf16=proof['text_length_utf16'])))
        return (await self.socket.next('doc_positions'))['reserved']

    async def release(self, identifier):
        await document_control.handle_frame(self.socket, {'id': self.user_id}, dict(
            type='doc_release', artifact_id=self.entry.artifact_id, store_id=self.entry.live.store.store_id,
            client_id=identifier))

    def close(self):
        document_control._subscriptions.pop(self.socket, None)


def reserved_text(live, identifier):
    start, end = live.held_span(identifier)
    return live.text[start:end]


async def scenario(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    return manager, entry, Session(ownership_scope, entry)


async def finish(manager, session):
    session.close()
    await manager.close()
    app_loop.clear()


async def submit(entry, identifier, quote='b', start=2, end=3):
    """POST the comment the way the browser does: the quote, its offsets at capture, and the live-capture flag."""
    app = FastAPI()
    app.include_router(doc_comments.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.post(f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN},
            json=dict(delivery_policy="immediate", body='reserved', anchor_text=quote, from_off=start, to_off=end,
                      live_capture=True, client_id=identifier))


@pytest.mark.asyncio
async def test_insert_before_repeated_text_keeps_the_original_word(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('word', 0, 1)
        digest = snapshot(entry)['digest']
        typing(entry, 0, insert='a ')
        assert entry.live.held_span('word') == (2, 3) and 'word' not in entry.live.ranges
        response = await submit(entry, 'word', 'a', 0, 1)
        row = await run_in_db(comments.get_comment, 'word')
        assert response.status_code == 200, response.text
        assert (row['from_off'], row['to_off']) == (2, 3) and entry.live.range_text('word') == 'a'
        assert entry.live.held_span('word') is None and entry.live.ranges['word'] == (2, 3)
        assert digest != snapshot(entry)['digest']
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('edge,insert', [(2, 'X'), (3, 'X')])
async def test_insert_exactly_at_an_edge_stays_outside(ownership_scope, tmp_path, monkeypatch, edge, insert):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('edge', 2, 3)
        typing(entry, edge, insert=insert)
        assert reserved_text(entry.live, 'edge') == 'b'
        response = await submit(entry, 'edge')
        assert response.status_code == 200, response.text
        assert (await run_in_db(comments.get_comment, 'edge'))['anchor_text'] == 'b'
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_one_edit_maps_comment_ranges_and_reservations_each_to_its_own_span(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    live = entry.live
    reserved, tracked = {'held-a': (1, 3), 'held-b': (3, 6)}, {'kept-a': (0, 1), 'kept-b': (2, 5)}
    try:
        assert len(live.text) >= 6
        for identifier, (start, end) in reserved.items():
            assert await session.reserve(identifier, start, end)
        for identifier, (start, end) in tracked.items():
            live.track_range(identifier, start, end)
        before = {identifier: live.text[start:end] for identifier, (start, end) in {**reserved, **tracked}.items()}
        typing(entry, 0, insert='zz ')
        for identifier, (start, end) in reserved.items():
            assert live.held_span(identifier) == (start + 3, end + 3) and reserved_text(live, identifier) == before[identifier]
        for identifier, (start, end) in tracked.items():
            assert live.ranges[identifier] == (start + 3, end + 3) and live.range_text(identifier) == before[identifier]
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_edit_inside_the_range_refuses_and_releases(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('inside', 4, 10)
        typing(entry, 6, delete=1, insert='X')
        response = await submit(entry, 'inside', 'second', 4, 10)
        assert response.status_code == 409 and 'text you selected changed' in response.text
        assert 'inside' not in entry.live.ranges and entry.live.held_span('inside') is None
        assert 'select it again' in response.text.lower() and 'draft is kept' in response.text
        assert await run_in_db(comments.get_comment, 'inside') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_state_mismatch_reserves_nothing_then_a_fresh_state_reserves(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        stale = snapshot(entry)
        typing(entry, 0, insert='Z')
        assert not await session.reserve('late', 3, 4, **stale)
        assert entry.live.held_span('late') is None
        assert not await session.reserve('late', 3, 4, digest='0000000000000000')
        assert await session.reserve('late', 3, 4)
        assert reserved_text(entry.live, 'late') == 'b'
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_a_delete_with_an_unchanged_vector_is_not_the_browsers_state(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        stale = snapshot(entry)
        typing(entry, 0, delete=2)
        assert not await session.reserve('gone', 0, 1, **{k: stale[k] for k in ('digest', 'text_length_utf16')})
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_rollback_releases_the_reservation(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('undone', 2, 3)
        entered, release = hold(monkeypatch, 'undone', fail=True)
        release.set()
        with pytest.raises(OSError):
            await submit(entry, 'undone')
        assert 'undone' not in entry.live.ranges and entry.live.held_span('undone') is None
        assert await run_in_db(comments.get_comment, 'undone') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_the_holder_leaving_and_a_cancel_release(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    other = Session(ownership_scope, entry)
    try:
        assert await session.reserve('left', 2, 3) and await session.reserve('cancelled', 4, 10)
        await other.release('cancelled')  # another socket cannot cancel it
        assert entry.live.held_span('cancelled') is not None
        await session.release('cancelled')
        assert entry.live.held_span('cancelled') is None
        await document_control.handle_frame(session.socket, {'id': session.user_id}, dict(
            type='doc_close', artifact_id=entry.artifact_id, store_id=entry.live.store.store_id))
        assert entry.live.held_span('left') is None and not entry.live.ranges
    finally:
        other.close()
        await finish(manager, session)


@pytest.mark.asyncio
async def test_the_cap_is_per_holder_and_evicts_that_holders_oldest(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    other = Session(ownership_scope, entry)
    try:
        assert await other.reserve('theirs', 4, 10)
        for index in range(RESERVATION_LIMIT + 1):
            assert await session.reserve(f'r{index}', 2, 3)
        live = entry.live
        held = [name for name in ('theirs', *(f'r{index}' for index in range(RESERVATION_LIMIT + 1))) if live.held_span(name)]
        assert len(held) == RESERVATION_LIMIT + 1
        assert 'r0' not in held and 'r1' in held and f'r{RESERVATION_LIMIT}' in held and 'theirs' in held
        assert not entry.live.ranges
    finally:
        other.close()
        await finish(manager, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('vector', base64.b64encode(b'\x00').decode()),
    ('digest', '0000000000000000'),
    ('text_length_utf16', 999),
])
async def test_each_part_of_the_state_proof_is_checked_at_reserve_time(ownership_scope, tmp_path, monkeypatch, field, value):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert not await session.reserve('proof', 2, 3, **{field: value})
        assert entry.live.held_span('proof') is None
        assert await session.reserve('proof', 2, 3)
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_the_length_proof_counts_utf16_units(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        typing(entry, 0, insert='\U0001F600')
        text = entry.live.text
        assert len(text.encode('utf-16-le')) // 2 != len(text)
        assert not await session.reserve('units', 4, 5, text_length_utf16=len(text))
        assert await session.reserve('units', 4, 5)
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_a_reserve_for_another_store_is_refused_and_holds_nothing(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        proof = snapshot(entry)
        await document_control.handle_frame(session.socket, {'id': session.user_id}, dict(
            type='doc_positions', artifact_id=entry.artifact_id, store_id='another-store', seq=1, request_id='elsewhere',
            reserve=dict(client_id='store', from_off=2, to_off=3, vector=proof['vector'],
                         digest=proof['digest'], text_length_utf16=proof['text_length_utf16'])))
        frame = await session.socket.next('doc_error')
        assert frame['kind'] == 'store_mismatch' and frame['request_id'] == 'elsewhere'
        assert entry.live.held_span('store') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_a_second_reserve_under_the_same_id_replaces_the_span(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('again', 2, 3)
        assert await session.reserve('again', 4, 10)
        assert entry.live.held_span('again') == (4, 10)
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_a_duplicate_create_releases_the_reservation_held_under_its_id(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('twice', 2, 3)
        assert (await submit(entry, 'twice')).status_code == 200
        # The live document no longer tracks the stored comment, so its id can be reserved again.
        entry.live.ranges.pop('twice')
        assert await session.reserve('twice', 2, 3)
        assert (await submit(entry, 'twice')).status_code == 200
        assert entry.live.held_span('twice') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_evicting_the_document_drops_its_reservations(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('evicted', 2, 3)
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        assert entry.live.held_span('evicted') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_a_create_without_its_reservation_is_refused_and_creates_nothing(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        response = await submit(entry, 'never-reserved')
        assert response.status_code == 409 and 'Select it again; your draft is kept' in response.text
        assert await run_in_db(comments.get_comment, 'never-reserved') is None
        assert not entry.live.ranges and entry.live.held_span('never-reserved') is None
        assert await session.reserve('released', 2, 3)
        await session.release('released')
        assert (await submit(entry, 'released')).status_code == 409
        assert await run_in_db(comments.get_comment, 'released') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_another_authors_reservation_cannot_be_consumed(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        entry.live.reserve('theirs', 2, 3, 'someone-else', object(), vector=entry.live.doc.get_state(),
                           digest=entry.live.digest, length=len(entry.live.text.encode('utf-16-le')) // 2)
        response = await submit(entry, 'theirs')
        assert response.status_code >= 400 and await run_in_db(comments.get_comment, 'theirs') is None
        assert entry.live.reserved_by('theirs', 'someone-else') is not None
        proof = snapshot(entry)
        await document_control.handle_frame(session.socket, {'id': session.user_id}, dict(
            type='doc_positions', artifact_id=entry.artifact_id, store_id=entry.live.store.store_id, seq=1,
            request_id='mine', reserve=dict(client_id='theirs', from_off=2, to_off=3, vector=proof['vector'],
                                            digest=proof['digest'], text_length_utf16=proof['text_length_utf16'])))
        frame = await session.socket.next('doc_error')
        assert frame['request_id'] == 'mine' and 'already in use' in frame['message']
        assert entry.live.reserved_by('theirs', 'someone-else') is not None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_consumers_ignore_reserved_ids(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_comments import candidate_ranges
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        assert await session.reserve('quiet', 2, 3)
        live = entry.live
        assert live.held_span('quiet') is not None and 'quiet' not in live.ranges
        assert 'quiet' not in candidate_ranges(live, None)
        await document_control.handle_frame(session.socket, {'id': session.user_id}, dict(
            type='doc_positions', artifact_id=entry.artifact_id, store_id=live.store.store_id, seq=2))
        frame = await session.socket.next('doc_positions')
        assert all(item.get('id') != 'quiet' for item in frame.get('threads', []) + frame.get('ranges', []))
    finally:
        await finish(manager, session)


def reordered(vector: bytes) -> str:
    """The same state vector with its entries in the other client order, as base64.

    Yjs writes clients by id descending and the server in its own order, so one of the two orders always differs
    from the live encoding. Returns the one that does.
    """
    def varuint(value):
        out = bytearray()
        while value >= 0x80:
            out.append(value & 0x7F | 0x80)
            value >>= 7
        out.append(value)
        return bytes(out)

    clocks = state_clocks(vector)
    for descending in (True, False):
        encoded = varuint(len(clocks)) + b''.join(
            varuint(client) + varuint(clocks[client]) for client in sorted(clocks, reverse=descending))
        if encoded != vector:
            return base64.b64encode(encoded).decode()
    raise AssertionError('a vector with fewer than two clients has one order')


@pytest.mark.asyncio
async def test_a_reservation_accepts_the_same_state_in_another_client_order(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        typing(entry, 0, insert='X')
        typing(entry, 1, insert='Y')
        live = entry.live
        assert len(state_clocks(live.doc.get_state())) >= 2
        vector = reordered(live.doc.get_state())
        assert vector != snapshot(entry)['vector']
        assert await session.reserve('plain', 4, 5, vector=vector)
        quote = reserved_text(live, 'plain')
        response = await submit(entry, 'plain', quote, 4, 5)
        assert response.status_code == 200, response.text
        assert (await run_in_db(comments.get_comment, 'plain'))['anchor_text'] == quote
    finally:
        await finish(manager, session)
