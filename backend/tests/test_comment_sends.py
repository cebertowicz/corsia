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
            "Malformed message input" if path == "main" else "Malformed subagent input"
        )
