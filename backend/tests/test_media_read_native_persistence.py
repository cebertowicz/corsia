"""Native media persistence, checkpoint, and provider-grammar contracts."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image


def _png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="red").save(stream, format="PNG")
    return stream.getvalue()


def _write(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.write_bytes(data)
    return path


def test_media_tool_result_deepcopy_preserves_reference(tmp_path) -> None:
    """Native checkpoint snapshots can clone an in-flight media result."""
    from core.media_reads import MediaToolResult, media_read_reference

    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))
    result = MediaToolResult(reference.placeholder_text(), reference)

    cloned = copy.deepcopy(result)

    assert cloned == result
    assert cloned.media == reference


def _target(*, vision: bool):
    return SimpleNamespace(provider="anthropic", display_name="claude-sonnet-5", supports_vision=vision)


@pytest.mark.asyncio
async def test_native_many_image_request_relimits_existing_blocks(
    monkeypatch, tmp_path,
) -> None:
    """Crossing Anthropic's threshold resizes every image in the request."""
    from core.context_builder import ContextBuilder
    from core import turns_steps

    stream = BytesIO()
    Image.new("RGB", (2001, 8), color="red").save(stream, format="PNG")
    encoded = base64.b64encode(stream.getvalue()).decode("ascii")

    class FakeImage:
        def data_url(self) -> str:
            return f"data:image/png;base64,{encoded}"

    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(
        turns_steps,
        "_native_tool_media",
        lambda canonical, _model, image_count: SimpleNamespace(
            text=canonical.content,
            image=FakeImage(),
        ),
    )
    existing = {
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{encoded}",
                },
            }
            for _index in range(20)
        ],
    }
    canonical = SimpleNamespace(
        role="tool",
        content="image-21",
        media_metadata={
            "media": {
                "path": "/tmp/shot.png",
                "mime_type": "image/png",
                "kind": "image",
                "byte_size": 1,
                "sha256": "a" * 64,
            }
        },
    )

    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [existing],
        [(SimpleNamespace(id="call-21", name="read_file", arguments={}), "image-21", canonical)],
        "thread-1",
        512,
        "anthropic/selected",
    )

    image_urls = [
        block["image_url"]["url"]
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "image_url"
    ]
    assert len(image_urls) == 21
    first_bytes = base64.b64decode(image_urls[0].partition(",")[2])
    with Image.open(BytesIO(first_bytes)) as image:
        assert max(image.size) == 2000


@pytest.mark.asyncio
async def test_native_tool_complete_persists_media_reference(
    isolated_db, monkeypatch, tmp_path,
) -> None:
    """The native canonical row carries the reference its delivery path needs."""
    from core.media_reads import MediaToolResult, media_read_reference
    from core.step_handler import StepHandler
    from core.turn_core import TurnIdentity
    from infra.database import get_db

    thread_id = "native-media-persistence"
    turn_id = "13240000-0000-4000-8000-000000000001"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "owner", "openai/test", "t", "t"),
        )
        db.commit()
    finally:
        db.close()

    async def broadcast(_payload: dict) -> None:
        return None

    handler = StepHandler(
        thread_id,
        {"model": "openai/test", "model_name": "Test"},
        broadcast,
        turn_id,
    )
    handler.bind_turn_identity(TurnIdentity(turn_id, 1))
    await handler.handle("tool_calls_start", {
        "tools": [{
            "id": "call-1", "name": "read_file", "hint": "read", "args": "{}",
        }],
        "content": "",
        "tool_call_dicts": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    })
    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))
    result = MediaToolResult(reference.placeholder_text(), reference)

    canonical = await handler.handle("tool_complete", {
        "id": "call-1", "name": "read_file", "result": result,
    })

    assert canonical.media_metadata == {"media": reference.as_metadata()}
    db = get_db()
    try:
        row = db.execute(
            "SELECT content, metadata FROM messages WHERE id=?",
            (canonical.message_id,),
        ).fetchone()
    finally:
        db.close()
    metadata = json.loads(row["metadata"])
    assert metadata["media"] == reference.as_metadata()
    assert "base64" not in row["content"]
    assert "base64" not in row["metadata"]

    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, *, db: _target(vision=True),
    )
    from core.media_delivery import tool_result_media_for_model

    delivered = tool_result_media_for_model(canonical, "openai/test")

    assert delivered is not None
    assert delivered.image is not None
    assert delivered.image.data == _png_bytes()


@pytest.mark.asyncio
async def test_native_checkpoint_source_excludes_tool_media_bytes(monkeypatch) -> None:
    """Provider-only tool media never enters the resumable checkpoint source."""
    from core import turn_iteration

    async def execute_tools(
        _agent, _response, messages, *_args, **kwargs,
    ):
        assert kwargs["model"] == "openai/selected"
        return [
            *messages,
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "[Image: shot.png]"},
            {
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,c2VjcmV0"},
                }],
            },
        ]

    monkeypatch.setattr(turn_iteration.turns_steps, "execute_tool_calls", execute_tools)
    state = SimpleNamespace(
        messages=[{"role": "user", "content": "start"}],
        tools_used=[],
        checkpoint_source_messages=[],
        turn_model="openai/selected",
        iteration=0,
        fit_ledger=None,
    )

    stopped = await turn_iteration._run_tool_phase(
        SimpleNamespace(max_iterations=5),
        state,
        SimpleNamespace(),
        False,
        "thread-1",
        asyncio.Event(),
        AsyncMock(),
        None,
        None,
    )

    assert stopped is False
    assert [message["role"] for message in state.checkpoint_source_messages] == [
        "user",
        "assistant",
    ]
    assert "base64" not in str(state.checkpoint_source_messages)


def test_native_tool_result_appends_image_using_proven_user_grammar(
    tmp_path,
) -> None:
    """Native reuses the same OpenAI-shaped image_url blocks send-time uses."""
    from core.context_builder import ContextBuilder
    from core.media_delivery import tool_result_media_content
    from core.media_reads import media_read_reference

    data = _png_bytes()
    reference = media_read_reference(_write(tmp_path, "shot.png", data))
    media = tool_result_media_content(
        "[Image: shot.png]", reference, target=_target(vision=True)
    )

    context = ContextBuilder(workspace=tmp_path)
    messages = context.add_tool_result(
        [], "call-1", "read_file", media.text
    )
    messages = context.add_tool_media(messages, [media.image])

    assert messages[0]["role"] == "tool"
    assert messages[0]["content"] == "[Image: shot.png]"
    assert messages[1]["role"] == "user"
    blocks = messages[1]["content"]
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_native_tool_result_without_media_is_unchanged(tmp_path) -> None:
    """Adjacent regression: text tool results still append exactly one message."""
    from core.context_builder import ContextBuilder

    messages = ContextBuilder(workspace=tmp_path).add_tool_result(
        [], "call-1", "read_file", "plain result"
    )

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": "plain result",
        }
    ]


def test_anthropic_translation_keeps_image_blocks_for_media_follow_up() -> None:
    """The proven native grammar survives translation to Anthropic blocks."""
    from llm.providers.anthropic_translation import (
        _translate_messages_for_anthropic_sdk,
    )

    encoded = base64.b64encode(_png_bytes()).decode()
    _system, out = _translate_messages_for_anthropic_sdk([
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": "[Image: shot.png]",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                },
            ],
        },
    ])

    assert out[0]["content"][0]["type"] == "tool_result"
    assert out[1]["content"][0]["type"] == "image"
    assert out[1]["content"][0]["source"]["media_type"] == "image/png"
