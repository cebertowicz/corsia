"""A chat-attached document reaches every harness with the path it is saved at.

Each case drives the real send path (``send_thread_input`` through preflight and
dispatch) and captures the exact text the provider call receives, so a note that
is built but never delivered cannot pass.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from shared_send_fixtures import _insert_native_model, _insert_session
from vendor_account_fixtures import signed_in_default

pytestmark = pytest.mark.usefixtures("signed_in_defaults")

_THREAD = "attach-note-thread"
_PDF = {"file_id": "pdf-1", "filename": "spec.pdf", "mime_type": "application/pdf"}
_BIN = {
    "file_id": "bin-1", "filename": "blob.bin",
    "mime_type": "application/octet-stream",
}


def _notes(tmp_path) -> str:
    base = tmp_path / ".threads" / _THREAD / "uploads"
    return (
        f"[Document attached: spec.pdf, saved at {base / 'pdf-1' / 'spec.pdf'}]\n"
        f"[File attached: blob.bin, saved at {base / 'bin-1' / 'blob.bin'}]"
    )


def _register_uploads(tmp_path, monkeypatch):
    from infra import config
    from registry import models as registry_models

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    for attachment in (_PDF, _BIN):
        path = (
            tmp_path / ".threads" / _THREAD / "uploads"
            / attachment["file_id"] / attachment["filename"]
        )
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        registry_models.register_artifact(
            integration="upload",
            type="upload",
            external_id=attachment["file_id"],
            parent_thread_id=_THREAD,
            path=str(path),
        )


def _thread_row(model: str) -> dict:
    from infra.database import get_db

    _insert_session(_THREAD, user_id="u-test", model=model, spawned_by=None)
    db = get_db()
    try:
        harness = model.split("/")[0]
        if harness in ("claude-code", "codex"):
            signed_in_default(harness, db=db)
        return dict(db.execute(
            "SELECT * FROM threads WHERE id=?", (_THREAD,),
        ).fetchone())
    finally:
        db.close()


async def _accept(*_args, **kwargs):
    if "persist_input_once" in kwargs:
        await kwargs["persist_input_once"]()
    if "bind_input_turn" in kwargs:
        await kwargs["bind_input_turn"](kwargs["preadmitted_turn"].turn_id, None)
    if "mark_accepted" in kwargs:
        kwargs["mark_accepted"]()
    await kwargs["settle_delivery"]()
    return kwargs["preadmitted_turn"]


async def _send(thread_row, content="read these"):
    from infra import send_service

    return await send_service.send_thread_input(
        _THREAD, thread_row, content, user={"id": "u-test"},
        native_mode="delegation", attachments=[dict(_PDF), dict(_BIN)],
    )


@pytest.mark.asyncio
async def test_cc_receives_saved_path_note_once(isolated_db, monkeypatch, tmp_path):
    from connections import models
    from subagents import cc_runtime

    _register_uploads(tmp_path, monkeypatch)
    row = _thread_row("claude-code/sonnet")
    monkeypatch.setattr(models, "connection_available", lambda _name: True)
    monkeypatch.setattr(cc_runtime, "is_live", lambda _thread_id: False)
    feed = AsyncMock(side_effect=_accept)
    monkeypatch.setattr(cc_runtime, "feed_main", feed)

    await _send(row)

    text = feed.call_args.args[1]
    assert text.count(_notes(tmp_path)) == 1
    assert text.index(_notes(tmp_path)) < text.rindex("[To reply")
    assert text.count("saved at") == 2
    rendered = feed.call_args.kwargs["prepared_context"].rendered
    assert rendered.fit_current_input.count(_notes(tmp_path)) == 1


@pytest.mark.asyncio
async def test_codex_receives_saved_path_note_once(isolated_db, monkeypatch, tmp_path):
    from subagents import codex_runtime

    _register_uploads(tmp_path, monkeypatch)
    row = _thread_row("codex/gpt-5-codex")
    monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
    monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=False))
    feed = AsyncMock(side_effect=_accept)
    monkeypatch.setattr(codex_runtime, "feed_main", feed)

    await _send(row)

    text = feed.call_args.args[1]
    assert text.count(_notes(tmp_path)) == 1
    assert text.index(_notes(tmp_path)) < text.rindex("[To reply")
    assert text.count("saved at") == 2
    rendered = feed.call_args.kwargs["prepared_context"].rendered
    assert rendered.fit_current_input.count(_notes(tmp_path)) == 1


@pytest.mark.asyncio
async def test_native_model_input_carries_saved_path_note_once(
    isolated_db, monkeypatch, tmp_path
):
    from core import turn_context
    from shared_send_fixtures import _install_fake_native_context_agent

    _install_fake_native_context_agent(monkeypatch)
    _register_uploads(tmp_path, monkeypatch)
    _insert_native_model("native/test")
    row = _thread_row("native/test")
    seen: list[str] = []
    real = turn_context.prepare_native_context_payload

    async def capture(**kwargs):
        seen.append(kwargs["user_content"])
        return await real(**kwargs)

    monkeypatch.setattr(turn_context, "prepare_native_context_payload", capture)
    async def accept_native(*args, **kwargs):
        identity = await _accept(*args, **kwargs)
        return "native_delegation", None, identity

    delivered = AsyncMock(side_effect=accept_native)
    monkeypatch.setattr("infra.send_dispatch.dispatch_native_delegation", delivered)

    await _send(row)

    assert seen[-1].count(_notes(tmp_path)) == 1
    assert seen[-1].index(_notes(tmp_path)) < seen[-1].rindex("[To reply")
    ctx_content = delivered.call_args.args[2]
    assert ctx_content.count("saved at") == 2
