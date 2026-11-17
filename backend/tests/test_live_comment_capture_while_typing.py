"""A capture made while someone else types attaches: a recent state maps forward, and only its own text decides."""
import base64

import pytest

from doceditor import comments, live_states
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone, state_clocks, text_digest
from infra.db_async import run_in_db
from tests.test_document_live_suggestions import typing
from tests.test_live_comment_creation import snapshot
from tests.test_live_comment_reservation import finish, reserved_text, scenario, submit
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_control


def selection(entry, quote):
    start = entry.live.text.index(quote)
    return start, start + len(quote)


@pytest.mark.asyncio
async def test_a_capture_from_a_state_the_typing_has_moved_past_attaches_to_its_text(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        start, end = selection(entry, 'second')
        before_any_ask = snapshot(entry)
        typing(entry, 0, insert='Z')
        # The first ask names a state from before anyone asked, which nothing kept; the browser asks again.
        assert not await session.reserve('late', start, end, 'second', **before_any_ask)
        asked = snapshot(entry)
        typing(entry, 0, insert='Y')
        typing(entry, len(entry.live.text), insert=' more')
        assert await session.reserve('late', start + 1, end + 1, 'second', **asked)
        assert entry.live.held_span('late') == (start + 2, end + 2) and reserved_text(entry.live, 'late') == 'second'
        typing(entry, 0, insert='X')
        response = await submit(entry, 'late', 'second', start + 1, end + 1)
        assert response.status_code == 200, response.text
        row = await run_in_db(comments.get_comment, 'late')
        assert (row['from_off'], row['to_off']) == (start + 3, end + 3) and entry.live.range_text('late') == 'second'
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_a_past_state_without_its_quote_is_refused(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        start, end = selection(entry, 'second')
        assert await session.reserve('first', start, end)
        asked = snapshot(entry)
        typing(entry, 0, insert='Z')
        assert not await session.reserve('late', start, end, **asked)
        assert entry.live.held_span('late') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_an_edit_inside_the_selection_since_its_state_is_refused(ownership_scope, tmp_path, monkeypatch):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        start, end = selection(entry, 'second')
        assert await session.reserve('first', start, end)
        asked = snapshot(entry)
        typing(entry, start + 2, insert='X')
        assert not await session.reserve('late', start, end, 'second', **asked)
        assert entry.live.held_span('late') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('bound', ['count', 'age'])
async def test_a_state_older_than_the_kept_ones_is_refused(ownership_scope, tmp_path, monkeypatch, bound):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(live_states, 'monotonic', lambda: now[0])
    try:
        start, end = selection(entry, 'second')
        assert await session.reserve('first', start, end)
        asked = snapshot(entry)
        typing(entry, 0, insert='Z')
        if bound == 'count':
            entry.live.states.limit = 2
            typing(entry, 0, insert='Y')
        else:
            now[0] += live_states.PAST_STATE_SECONDS + 1
            typing(entry, len(entry.live.text), insert=' more')
        shift = len(entry.live.text[:entry.live.text.index('second')]) - start
        assert not await session.reserve('late', start, end, 'second', **asked)
        assert entry.live.held_span('late') is None
        assert await session.reserve('late', start + shift, end + shift, 'second')
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
async def test_equal_length_deletes_in_another_order_are_refused_not_anchored_elsewhere(ownership_scope, tmp_path, monkeypatch):
    """The browser applied its own delete before a remote one of the same length.

    Its text never existed on the server, yet its clocks and length equal a state the server held. Mapped from
    that state, its selection would land one character early on the same quote; the digest refuses it.
    """
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    live = entry.live
    try:
        assert await session.reserve('first', *selection(entry, 'second'))
        typing(entry, 0, insert='XaaaaY')
        browser = clone(live.doc.get_update())
        vector = browser.get_state()
        del browser['text'][0:1]  # the browser's own delete: X
        typing(entry, 5, delete=1, insert='')  # a remote delete the server applies first: Y
        server_held = snapshot(entry)
        live.receive(BrowserMessage('browser-delete', browser.get_update(vector)))
        browser_text = str(browser['text'])
        assert browser_text.startswith('aaaaY') and live.text.startswith('aaaa')
        browser_state = dict(vector=base64.b64encode(browser.get_state()).decode(), digest=text_digest(browser_text),
                             text_length_utf16=len(browser_text.encode('utf-16-le')) // 2)
        assert state_clocks(browser.get_state()) == state_clocks(live.doc.get_state())
        assert browser_state['text_length_utf16'] == server_held['text_length_utf16']
        assert browser_state['digest'] != server_held['digest']
        # The browser's 'aa' at 1..3 is the server's 1..3 now; read as the server's 'Xaaaa' it would become 0..2.
        assert not await session.reserve('late', 1, 3, 'aa', **browser_state)
        assert live.held_span('late') is None
    finally:
        await finish(manager, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('quote', ['second' * 1000, 'secon\U0001F600'], ids=['characters', 'utf16-units'])
async def test_a_quote_longer_than_its_selection_is_refused_before_any_compare(ownership_scope, tmp_path, monkeypatch,
                                                                              quote):
    manager, entry, session = await scenario(ownership_scope, tmp_path, monkeypatch)
    try:
        start, end = selection(entry, 'second')
        proof = snapshot(entry)
        await document_control.handle_frame(session.socket, {'id': session.user_id}, dict(
            type='doc_positions', artifact_id=entry.artifact_id, store_id=entry.live.store.store_id, seq=1,
            request_id='long', reserve=dict(client_id='long', from_off=start, to_off=end, quote=quote, vector=proof['vector'],
                                            digest=proof['digest'], text_length_utf16=proof['text_length_utf16'])))
        frame = await session.socket.next('doc_error')
        assert frame['request_id'] == 'long' and 'longer than its selection' in frame['message']
        assert entry.live.held_span('long') is None
        # A quote exactly the selection's length still reserves.
        assert await session.reserve('exact', start, end, 'second')
    finally:
        await finish(manager, session)
