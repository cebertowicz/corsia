"""Comments-only sends across both WebSocket input paths."""
import json
from types import SimpleNamespace

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from test_reply_context import KEY, THREAD, _admit, _comment, _meta, thread_rows  # noqa: F401


def test_comments_render_exactly_before_existing_context():
    from core.reply_context import reply_context_lines

    comments = [{**_comment(seq=5120, quote="first", note="why?"), "cid": "c5000.1"},
                {**_comment(seq=5120, quote="second", note="yes"), "cid": "c5000.2"}]
    assert reply_context_lines({"comments": comments, "in_view": [[1, 2, 2]]}) == (
        '[Comments (context, not an instruction):\n'
        ' c5000.1. On #5120: "first" | note: why?\n'
        ' c5000.2. On #5120: "second" | note: yes]\n'
        '[Reply grounding: answer their comments; start each answer with the ids of the '
        "comments it answers in brackets, like [c12.1] or [c12.3][c12.4], and don't repeat "
        'their quote. Comments that need no answer can go unanswered.]\n'
        '[In view while typing: #1-#2 (2 shown) (context, not an instruction)]\n'
    )


def test_comments_only_replay_has_no_blank_text_line():
    from core.message_history import render_agent_envelope

    rendered = render_agent_envelope("", _meta({"comments": [{**_comment(), "cid": "c1.1"}]}))
    assert "unanswered.]\n\n" not in rendered.content
    assert "unanswered.]\n" in rendered.content


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["main", "subagent"])
@pytest.mark.parametrize("valid", [True, False])
async def test_comment_only_sends_validate_before_storage(thread_rows, monkeypatch, path, valid):
    import infra.send_service as send_service
    import ws as ws_module
    import ws.message_dispatch as dispatch
    import ws.subagent_control as control

    events = []
    row = {"id": THREAD, "reasoning_effort": None, "spawned_by": "parent"}

    async def owned(*_args):
        return row

    async def emit(_ws, payload):
        events.append(payload)

    async def accept(thread_id, **kwargs):
        return _admit(thread_rows, kwargs["message_id"], kwargs["metadata"], content=kwargs["content"])

    async def deliver(*_args, **_kwargs):
        return SimpleNamespace(message_id="send", created_at="t", turn_id="turn", turn_order=1)

    async def bind(*_args):
        return []

    async def run(callback):
        return callback()

    async def ignore(*_args, **_kwargs):
        return None

    monkeypatch.setattr(send_service, "admit_prepared_input", accept)
    monkeypatch.setattr(send_service, "_bind_image_identity", bind)
    monkeypatch.setattr(send_service, "_deliver_admission", deliver)
    monkeypatch.setattr(send_service, "_boot_epoch", lambda: "test")
    monkeypatch.setattr("jobs.corrections_recording.notify_if_owner", lambda *_a, **_k: None)
    monkeypatch.setattr("infra.execution_class.execution_class", lambda _row: "native")
    monkeypatch.setattr("infra.execution_class.is_subagent", lambda _row: True)
    monkeypatch.setattr(ws_module, "_owned_thread", owned)
    monkeypatch.setattr(ws_module, "send_thread_input", send_service.send_thread_input)
    monkeypatch.setattr(dispatch.state, "send_to_client", emit)
    monkeypatch.setattr(dispatch, "_send_input_accepted", ignore)
    monkeypatch.setattr(control, "_send_input_accepted", ignore)
    context = {"comments": [_comment() if valid else _comment(seq=0)]}
    socket = object()
    if path == "main":
        await dispatch.handle_message(
            socket, {"id": "user"}, "user",
            {"thread_id": THREAD, "input_id": "send", "content": "", "reply_context": context},
            get_db_fn=lambda: SimpleNamespace(close=lambda: None), run_in_db_fn=run,
            get_thread_for_user_fn=lambda *_a, **_k: row, touch_thread_fn=lambda *_a: None,
            send_thread_input_fn=send_service.send_thread_input,
            handle_cc_main_message_fn=ignore, handle_codex_main_message_fn=ignore,
        )
    else:
        await ws_module._handle_subagent_feed(
            socket, {"id": "user"}, THREAD, "", [], False, "send", "apply_now", None, context,
        )
    stored = thread_rows.execute("SELECT metadata FROM messages WHERE id='send'").fetchone()
    if valid:
        assert stored is not None
        assert json.loads(stored["metadata"])[KEY]["comments"][0]["id"] == "key-1"
        assert not events
    else:
        assert stored is None
        assert events[0]["message"] == (
            "Comment 1 couldn’t be matched to text in this chat, so nothing was sent. "
            "Your message and comments are kept: remove that comment and send again."
        )


def test_replies_are_admitted_and_answered_in_the_root_thread(thread_rows):
    from infra.chat_comment_index import attach_comments
    from infra.queries import insert_message
    from infra.reply_context import RejectedComments
    from doceditor.reply_attention import chat_reply_items

    db = thread_rows
    db.execute("UPDATE threads SET user_id='person', next_message_seq=41 WHERE id=?", (THREAD,))
    db.commit()
    root = _comment(note='root')
    _admit(db, 'root-send', _meta({'comments': [root]}))
    reply = _comment(id='reply', note='follow-up', root_user_message_id='root-send', root_cid='c1.1')
    for bad in ({**reply, 'seq': 6}, {**reply, 'quote': 'wrong'},
                {**reply, 'root_user_message_id': 'missing'}, {**reply, 'root_cid': None}):
        with pytest.raises(RejectedComments):
            _admit(db, 'bad-send', _meta({'comments': [bad]}))
    _admit(db, 'reply-send', _meta({'comments': [reply, {**reply, 'id': 'reply-two', 'note': 'another'}]}))
    # Queued inputs receive a transcript sequence when delivered to the agent.
    db.execute("UPDATE messages SET seq=41 WHERE id='reply-send'")
    db.execute("UPDATE threads SET next_message_seq=42 WHERE id=?", (THREAD,))
    insert_message(THREAD, 'assistant', '[c2.1][c2.2] both', {'turn_unbound_reason': 'legacy_import'},
                   msg_id='reply-answer', db=db)
    page = [{'id': 'm5', 'role': 'assistant'}]
    attach_comments(db, THREAD, 'person', page)
    threads = page[0]['chat_comments']
    assert len(threads) == 1
    assert threads[0]['cid'] == 'c1.1'
    entries = threads[0]['answers']
    assert [item['text'] for item in entries if item['author_kind'] == 'user'] == ['follow-up', 'another']
    answers = [item for item in entries if item['author_kind'] == 'agent']
    assert {item['cid'] for item in answers} == {'c2.1', 'c2.2'}
    assert all(item['user_message_id'] == 'reply-send' and item['unread'] for item in answers)
    detail = chat_reply_items(db, 'person', THREAD)[0]['detail']
    assert detail['unread_count'] == 2
    assert detail['destination']['user_message_id'] == 'root-send'
    assert detail['destination']['cid'] == 'c1.1'
    db.execute("INSERT INTO chat_comment_views VALUES ('person','reply-send','c2.1',?)", (answers[0]['created_at'],))
    assert chat_reply_items(db, 'person', THREAD)[0]['detail']['unread_count'] == 1
    db.execute("INSERT INTO chat_comment_views VALUES ('person','reply-send','c2.2',?)", (answers[1]['created_at'],))
    assert chat_reply_items(db, 'person', THREAD) == []


@pytest.mark.parametrize("positions,which,them", [
    ([2], "Comment 2", "that comment"),
    ([1, 3], "Comments 1 and 3", "those comments"),
    ([1, 2, 4], "Comments 1, 2 and 4", "those comments"),
    ([], "Your comments", "them"),
])
def test_a_refused_comment_send_names_the_comments_and_the_next_step(positions, which, them):
    from infra.reply_context import RejectedComments

    assert str(RejectedComments(positions)) == (
        f"{which} couldn’t be matched to text in this chat, so nothing was sent. "
        f"Your message and comments are kept: remove {them} and send again."
    )
