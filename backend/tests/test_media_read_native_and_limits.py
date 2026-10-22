"""Codex and native media-delivery contracts."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from codex_runtime_fixtures import declare_codex_tool_step


def _png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="red").save(stream, format="PNG")
    return stream.getvalue()


def _write(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.write_bytes(data)
    return path


def _target(*, vision: bool):
    return SimpleNamespace(
        provider="anthropic",
        display_name="claude-sonnet-5",
        supports_vision=vision,
    )


def test_codex_dynamic_tool_response_carries_input_image_before_text(
    tmp_path,
) -> None:
    """Codex DynamicToolCallResponse uses its proven inputImage content item."""
    from core.media_delivery import codex_tool_content_items, tool_result_media_content
    from core.media_reads import media_read_reference

    data = _png_bytes()
    reference = media_read_reference(_write(tmp_path, "shot.png", data))
    media = tool_result_media_content(
        "[Image: shot.png]", reference, target=_target(vision=True)
    )

    items = codex_tool_content_items(media)

    assert [item["type"] for item in items] == ["inputImage", "inputText"]
    assert items[0]["imageUrl"].startswith("data:image/png;base64,")
    assert items[1]["text"] == "[Image: shot.png]"


def test_codex_dynamic_tool_response_stays_text_only_without_media() -> None:
    """Adjacent regression: a normal Codex tool result keeps its text grammar."""
    from core.media_delivery import codex_tool_content_items

    items = codex_tool_content_items(None, text="plain result")

    assert items == [{"type": "inputText", "text": "plain result"}]


@pytest.mark.asyncio
async def test_codex_tool_execution_resolves_capability_by_catalog_id(
    monkeypatch,
) -> None:
    """Codex sends canonical model identity, not its bare transport selector."""
    from subagents.codex_app_server import CodexAppServerSession

    execute = AsyncMock(return_value="plain result")
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        model="gpt-5.6-luna",
        catalog_model_id="codex/gpt-5.6-luna",
        tool_registry=object(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=SimpleNamespace(turn_id="turn-1"),
    )
    declare_codex_tool_step(session, "call-1", persist=False)

    result, error = await session._handle_dynamic_tool_request({
        "tool": "read_file",
        "arguments": {"path": "shot.png"},
        "callId": "call-1",
    })

    assert error is None
    assert result["success"] is True
    assert execute.await_args.kwargs["model"] == "codex/gpt-5.6-luna"


@pytest.mark.asyncio
async def test_checkpoint_media_is_projected_before_harness_translation(
    monkeypatch, tmp_path,
) -> None:
    """Checkpoint tools expose projected media, not a raw reference."""
    from core.media_reads import MediaToolResult, media_read_reference
    from subagents import harness_tool_bridge

    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))
    raw = MediaToolResult(reference.placeholder_text(), reference)
    registry = SimpleNamespace(
        get=lambda _name: None, execute=AsyncMock(return_value=raw),
    )
    monkeypatch.setattr(
        "infra.database.get_db",
        lambda: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, db: _target(vision=True),
    )

    result = await harness_tool_bridge.execute_tool(
        registry,
        thread_id="thread-1",
        fork_id="checkpoint-1",
        model="anthropic/claude-sonnet-5",
        name="read_file",
        arguments={"path": "shot.png"},
    )

    assert isinstance(result, harness_tool_bridge.ProjectedMediaResult)
    assert not isinstance(result, harness_tool_bridge.ToolBridgeResult)
    assert result.media.image is not None
    assert result.media.image.mime_type == "image/png"


@pytest.mark.asyncio
async def test_checkpoint_media_materialization_runs_off_event_loop(
    monkeypatch, tmp_path,
) -> None:
    """Harness media hashing/resizing cannot stall the async tool bridge."""
    from core.media_delivery import ToolMediaContent
    from core.media_reads import MediaToolResult, media_read_reference
    from subagents import harness_tool_bridge

    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))
    raw = MediaToolResult(reference.placeholder_text(), reference)
    event_loop_thread = threading.get_ident()
    observed_threads = []

    registry = SimpleNamespace(
        get=lambda _name: None, execute=AsyncMock(return_value=raw),
    )

    def capture(text, _reference, _model):
        observed_threads.append(threading.get_ident())
        return ToolMediaContent(text=text, image=None)

    monkeypatch.setattr(
        "core.media_delivery.tool_result_media_for_reference",
        capture,
    )

    await harness_tool_bridge.execute_tool(
        registry,
        thread_id="thread-1",
        fork_id="checkpoint-1",
        model="anthropic/claude-sonnet-5",
        name="read_file",
        arguments={"path": "shot.png"},
    )

    assert observed_threads
    assert observed_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_native_media_materialization_runs_off_event_loop(
    monkeypatch, tmp_path,
) -> None:
    """Native media hashing/resizing cannot stall the async turn loop."""
    from core.context_builder import ContextBuilder
    from core import turns_steps

    event_loop_thread = threading.get_ident()
    observed_threads = []
    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )

    def capture(canonical, _model, image_count=1):
        observed_threads.append(threading.get_ident())
        return SimpleNamespace(text=canonical.content, image=None)

    monkeypatch.setattr(turns_steps, "_native_tool_media", capture)
    canonical = SimpleNamespace(
        role="tool",
        content="[Image: shot.png]",
        media_metadata={
            "media": {
                "path": str(tmp_path / "shot.png"),
                "mime_type": "image/png",
                "kind": "image",
                "byte_size": 1,
                "sha256": "a" * 64,
            }
        },
    )

    await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [],
        [(SimpleNamespace(id="call-1", name="read_file", arguments={}), str(canonical.content), canonical)],
        "thread-1",
        512,
        "anthropic/selected",
    )

    assert observed_threads
    assert observed_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_native_media_follows_every_tool_result_in_one_user_message(
    monkeypatch, tmp_path,
) -> None:
    """Parallel tool results stay contiguous before their media follow-up."""
    from core.context_builder import ContextBuilder
    from core import turns_steps

    class FakeImage:
        def __init__(self, value: str):
            self.value = value

        def data_url(self) -> str:
            return f"data:image/png;base64,{self.value}"

    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(
        turns_steps,
        "_native_tool_media",
        lambda canonical, _model, image_count=1: SimpleNamespace(
            text=canonical.content,
            image=FakeImage(canonical.content),
        ),
    )
    agent = SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path, model="model-1")
    canonical_results = [
        (
            SimpleNamespace(id=f"call-{index}", name="read_file", arguments={}),
            f"raw-{index}",
            SimpleNamespace(role="tool", content=f"image-{index}"),
        )
        for index in (1, 2)
    ]

    messages = await turns_steps._append_tool_results(
        agent, [], canonical_results, "thread-1", 512, "openai/selected",
    )

    assert [message["role"] for message in messages] == ["tool", "tool", "user"]
    assert [
        block["image_url"]["url"]
        for block in messages[-1]["content"]
    ] == [
        "data:image/png;base64,image-1",
        "data:image/png;base64,image-2",
    ]


@pytest.mark.asyncio
async def test_native_media_batch_counts_only_image_results(
    monkeypatch, tmp_path,
) -> None:
    """Every image in one native provider request sees the batch's total count."""
    from core.context_builder import ContextBuilder
    from core import turns_steps

    observed = []

    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )

    def capture(canonical, model, image_count):
        observed.append((canonical.content, model, image_count))
        return SimpleNamespace(text=canonical.content, image=None)

    monkeypatch.setattr(turns_steps, "_native_tool_media", capture)
    image_metadata = {
        "media": {
            "path": "/tmp/shot.png",
            "mime_type": "image/png",
            "kind": "image",
            "byte_size": 1,
            "sha256": "a" * 64,
            "filename": "shot.png",
        }
    }
    canonical_results = [
        (
            SimpleNamespace(id=f"call-{index}", name="read_file", arguments={}),
            f"image-{index}",
            SimpleNamespace(
                role="tool",
                content=f"image-{index}",
                media_metadata=image_metadata,
            ),
        )
        for index in range(21)
    ]
    canonical_results.append((
        SimpleNamespace(id="call-text", name="read_file", arguments={}),
        "plain",
        SimpleNamespace(role="tool", content="plain", media_metadata=None),
    ))

    await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [],
        canonical_results,
        "thread-1",
        512,
        "anthropic/selected",
    )

    assert [item[2] for item in observed] == [21] * 22


@pytest.mark.asyncio
async def test_native_media_count_includes_images_already_in_request(
    monkeypatch, tmp_path,
) -> None:
    """A later tool batch uses the full native provider-request image count."""
    from core.context_builder import ContextBuilder
    from core import turns_steps

    observed = []
    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )

    def capture(canonical, model, image_count):
        observed.append((model, image_count))
        return SimpleNamespace(text=canonical.content, image=None)

    monkeypatch.setattr(turns_steps, "_native_tool_media", capture)
    existing = {
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
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

    await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [existing],
        [(SimpleNamespace(id="call-21", name="read_file", arguments={}), "image-21", canonical)],
        "thread-1",
        512,
        "anthropic/selected",
    )

    assert observed == [("anthropic/selected", 21)]


def _unconvertible(_data: bytes, _mime: str) -> tuple[bytes, str]:
    raise ValueError("the bytes cannot be converted")


def test_an_unreadable_read_image_is_declined_in_terms_of_reading(
    tmp_path, monkeypatch
):
    """A file an agent read is never declined as something the user uploaded.

    The fitting seam takes both recovery actions from its caller precisely so a
    tool read cannot inherit an upload's advice, which is a dead end the user
    has no way to follow.
    """
    from core.media_delivery import _materialize_image
    from core.media_projection import MediaProjectionError
    from core.media_reads import media_read_reference

    monkeypatch.setattr(
        "core.image_resize.normalize_image_for_provider", _unconvertible
    )
    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))

    with pytest.raises(MediaProjectionError) as excinfo:
        _materialize_image(reference, target=_target(vision=True), image_count=1)

    assert excinfo.value.cause == "image_corrupt"
    assert excinfo.value.legal_action == "read_the_file_again"
    assert "upload" not in excinfo.value.legal_action


def test_an_oversized_read_image_is_declined_in_terms_of_reading(
    tmp_path, monkeypatch
):
    """The two declines stay distinct: this one is about size, still by reading."""
    from core.media_delivery import _materialize_image
    from core.media_projection import MediaProjectionError
    from core.media_reads import media_read_reference
    from infra import config

    monkeypatch.setitem(
        config.PROVIDER_IMAGE_LIMITS, "anthropic", {"max_bytes": 8, "max_dimension": None}
    )
    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))

    with pytest.raises(MediaProjectionError) as excinfo:
        _materialize_image(reference, target=_target(vision=True), image_count=1)

    assert excinfo.value.cause == "image_too_large"
    assert excinfo.value.legal_action == "read_a_smaller_image"
