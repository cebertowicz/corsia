from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import turn_provider_call
from test_media_delivery_adapters import _projection, _target


def _empty_replay(thread_id: str):
    from core.context_replay import ReplaySelection
    from history.types import PortableHistoryProjection

    projection = PortableHistoryProjection(
        format_version=1,
        thread_id=thread_id,
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=None,
        turns=(),
        full_raw_replay_available=True,
    )
    return ReplaySelection(
        mode="exact",
        history_epoch=0,
        sealed_boundary=None,
        tail=projection,
    )


async def _prepare_native_context(
    *,
    monkeypatch,
    tmp_path,
    thread_id: str,
    native_mode: str,
):
    from core.context_builder import ContextBuilder
    import infra.preflight_render as preflight_render
    from infra import send_service, send_preflight
    from shared_send_fixtures import _insert_session

    projection = _projection()
    target = _target("native")
    _insert_session(
        thread_id,
        user_id="owner",
        model=target.catalog_id,
        spawned_by=None,
    )
    agent = SimpleNamespace(
        model=target.transport_model,
        context=ContextBuilder(tmp_path),
        tools=SimpleNamespace(get_definitions=lambda: []),
        max_tokens=0,
        max_iterations=1,
        _connect_mcp=AsyncMock(),
    )
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    prepared = await preflight_render.render_preflight_selection(
        thread_id=thread_id,
        thread_row={
            "id": thread_id,
            "user_id": "owner",
            "project_id": None,
            "runtime_context_epoch": 0,
        },
        provider="native",
        target=target,
        current_user_content=f"inspect via {native_mode}",
        current_user_source_content=f"inspect via {native_mode}",
        attachments=[
            {
                "file_id": image.attachment_id,
                "filename": f"{image.attachment_id}.png",
                "mime_type": image.mime_type,
            }
            for image in projection.images
        ],
        media_projection=projection,
        selection=_empty_replay(thread_id),
        live_handle=True,
        input_identity=f"test-input:{thread_id}",
    )
    return agent, projection, target, prepared.rendered.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("native_mode", ["ws", "delegation", "event"])
async def test_native_prepared_context_provider_payload_keeps_ordered_images(
    isolated_db,
    monkeypatch,
    tmp_path,
    native_mode: str,
) -> None:
    from core import turn_iteration, turn_iteration_context, turns_steps
    from core.token_estimation import _estimate_messages_tokens

    thread_id = f"native-prepared-images-{native_mode}"
    agent, projection, target, frozen = await _prepare_native_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        thread_id=thread_id,
        native_mode=native_mode,
    )
    user_blocks = frozen.messages[-1]["content"]
    assert [block["type"] for block in user_blocks] == [
        "image_url",
        "image_url",
        "text",
    ]
    assert user_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert user_blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert frozen.context_usage["rendered_input_tokens"] == (
        _estimate_messages_tokens(list(frozen.messages))
    )

    provider_payloads = []

    async def call_llm(_agent, messages, *_args, **_kwargs):
        provider_payloads.append(messages)
        return None, False

    monkeypatch.setattr(turn_provider_call, "call_llm", call_llm)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    await turn_iteration.run_native_iteration(
        agent,
        f"inspect via {native_mode}",
        thread_id,
        "web",
        thread_id,
        AsyncMock(),
        asyncio.Event(),
        model=target.transport_model,
        turn_provider={},
        media=projection,
        prepared_context=frozen,
    )

    assert provider_payloads == [list(frozen.messages)]
    assert [
        block["type"] for block in provider_payloads[0][-1]["content"]
    ] == ["image_url", "image_url", "text"]
