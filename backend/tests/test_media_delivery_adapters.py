from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from core.media_projection import ValidatedImage, ValidatedMediaProjection
from core.turn_core import TurnIdentity


def _image_bytes(format_name: str, color: str) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=color).save(stream, format=format_name)
    return stream.getvalue()


def _projection() -> ValidatedMediaProjection:
    first = _image_bytes("PNG", "red")
    second = _image_bytes("JPEG", "blue")
    return ValidatedMediaProjection((
        ValidatedImage(
            attachment_id="image-first",
            mime_type="image/png",
            data=first,
            sha256="1" * 64,
            byte_size=len(first),
        ),
        ValidatedImage(
            attachment_id="image-second",
            mime_type="image/jpeg",
            data=second,
            sha256="2" * 64,
            byte_size=len(second),
        ),
    ), ("1" * 64, "2" * 64))


def _target(harness: str):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=f"{harness}/vision-test",
        harness=harness,
        provider="anthropic" if harness == "native" else harness,
        backend="api" if harness == "native" else harness,
        transport_model="vision-test",
        expected_reported_model="vision-test",
        context_window=1_000_000,
        available=True,
        availability_reason=None,
        display_name="vision-test",
        supports_reasoning=True,
        supports_vision=True,
    )


def _expected_codex_input(projection: ValidatedMediaProjection, text: str) -> list[dict]:
    return [
        *({"type": "image", "url": image.data_url()} for image in projection.images),
        {"type": "text", "text": text, "text_elements": []},
    ]


@pytest.mark.asyncio
async def test_codex_cold_turn_uses_ordered_typed_current_images() -> None:
    from subagents.codex_app_server import CodexAppServerSession

    projection = _projection()
    calls: list[tuple[str, dict]] = []

    async def request(method: str, params: dict, **_kwargs):
        calls.append((method, params))
        return {"turn": {"id": "provider-turn"}}

    session = CodexAppServerSession(binary="codex", task=None)
    session.thread_id = "provider-thread"
    session._request = request

    assert await session.start_turn("inspect both", media_projection=projection)

    assert calls == [(
        "turn/start",
        {
            "threadId": "provider-thread",
            "input": _expected_codex_input(projection, "inspect both"),
            "environments": [],
        },
    )]
    assert "image_url" not in str(calls)
    assert not hasattr(session, "media_projection")


@pytest.mark.asyncio
async def test_codex_warm_steer_uses_exact_turn_and_ordered_typed_images() -> None:
    from subagents.codex_app_server import CodexAppServerSession

    projection = _projection()
    calls: list[tuple[str, dict]] = []

    async def request(method: str, params: dict, **_kwargs):
        calls.append((method, params))
        return {}

    session = CodexAppServerSession(binary="codex", task=None)
    session.thread_id = "provider-thread"
    session._track_active_turn("provider-turn")
    session._request = request

    assert await session.steer("compare them", media_projection=projection)

    assert calls == [(
        "turn/steer",
        {
            "threadId": "provider-thread",
            "expectedTurnId": "provider-turn",
            "input": _expected_codex_input(projection, "compare them"),
        },
    )]
    assert "image_url" not in str(calls)


def test_native_user_content_consumes_projection_without_path_reads(monkeypatch) -> None:
    from core.context_builder import ContextBuilder
    from pathlib import Path

    projection = _projection()

    def forbidden_path_read(*_args, **_kwargs):
        raise AssertionError("validated image delivery must not read a filesystem path")

    monkeypatch.setattr(Path, "read_bytes", forbidden_path_read)
    content = ContextBuilder(Path("/unused"))._build_user_content(
        "identify the unique visual facts",
        projection,
        model="anthropic/claude-sonnet-4-6",
    )

    assert isinstance(content, list)
    assert [block["type"] for block in content] == [
        "image_url",
        "image_url",
        "text",
    ]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2] == {
        "type": "text",
        "text": "identify the unique visual facts",
    }
    assert "/unused" not in str(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_mode", ["ws", "delegation", "event"])
async def test_native_dispatch_modes_forward_same_projection(
    monkeypatch,
    native_mode: str,
) -> None:
    from infra import send_dispatch
    from infra.send_dispatch import DispatchContext, _accept_native

    projection = _projection()
    accepted = TurnIdentity("native-turn", 1)
    ws_dispatch = AsyncMock(return_value=("turn_engine", accepted))
    event_dispatch = AsyncMock(return_value=("turn_engine", AsyncMock(), accepted))
    delegation_dispatch = AsyncMock(
        return_value=("turn_engine", AsyncMock(), accepted)
    )
    monkeypatch.setattr(send_dispatch, "dispatch_native_ws", ws_dispatch)
    monkeypatch.setattr(send_dispatch, "dispatch_native_event", event_dispatch)
    monkeypatch.setattr(
        send_dispatch,
        "dispatch_native_delegation",
        delegation_dispatch,
    )

    ctx = DispatchContext(
        thread_id="native-session",
        thread_row={"model": "vision-test", "project_id": None, "user_id": "owner"},
        content="inspect",
        is_subagent=False,
        target=_target("native"),
        turn_id="native-turn",
        native_mode=native_mode,
        user={"id": "owner"},
        ws=object(),
        media_projection=projection,
    )

    await _accept_native(ctx)

    selected = {
        "ws": ws_dispatch,
        "delegation": delegation_dispatch,
        "event": event_dispatch,
    }[native_mode]
    assert selected.await_args.kwargs["media_projection"] is projection


@pytest.mark.asyncio
async def test_codex_dispatch_forwards_same_projection(monkeypatch) -> None:
    from infra import effort, send_dispatch
    from infra.send_dispatch import DispatchContext, _accept_codex

    projection = _projection()
    accepted = TurnIdentity("codex-turn", 1)
    adapter = AsyncMock(return_value=("codex_runtime.feed_main", accepted))
    monkeypatch.setattr(effort, "resolve_effort", lambda *_args: None)
    monkeypatch.setattr(send_dispatch, "dispatch_codex", adapter)

    ctx = DispatchContext(
        thread_id="codex-session",
        thread_row={"model": "codex/vision-test", "project_id": None, "user_id": "owner"},
        content="inspect",
        is_subagent=False,
        target=_target("codex"),
        turn_id="codex-turn",
        bind_input_turn=AsyncMock(),
        media_projection=projection,
    )

    await _accept_codex(ctx)

    assert adapter.await_args.kwargs["media_projection"] is projection


def test_fresh_ingress_ignores_caller_digest_and_keeps_it_private(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from core.media_delivery import (
        bind_projection_to_descriptor,
        build_delivery_projection,
        public_attachments,
    )

    from core.media_projection import MediaProjectionError
    from infra import config
    from registry import models
    from shared_send_fixtures import _insert_session

    owner_id = "owner"
    thread_id = "fresh-image-thread"
    _insert_session(thread_id, user_id=owner_id, spawned_by=None)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    image_path = tmp_path / ".threads" / thread_id / "uploads" / "fresh-image" / "visual.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_image_bytes("PNG", "green"))
    models.register_artifact(
        integration="upload",
        type="upload",
        external_id="fresh-image",
        parent_thread_id=thread_id,
        path=str(image_path),
    )
    attachment = {
        "file_id": "fresh-image",
        "filename": "visual.png",
        "mime_type": "image/png",
        "size": image_path.stat().st_size,
        "url": f"/api/uploads/{owner_id}/fresh-image/visual.png",
        "_corsia_validated_image_sha256": "0" * 64,
    }

    public, projection = build_delivery_projection(
        [attachment],
        owner_id=owner_id,
        thread_id=thread_id,
        target=_target("native"),
    )
    descriptor = bind_projection_to_descriptor(public, projection)

    assert "_corsia_validated_image_sha256" not in public[0]
    assert "_corsia_validated_image_sha256" not in str({"attachments": public})
    assert descriptor[0]["_corsia_validated_image_sha256"] == projection.digests[0]
    assert descriptor[0]["_corsia_validated_image_sha256"] != "0" * 64
    assert "_corsia_validated_image_sha256" not in str(
        public_attachments(descriptor)
    )

    image_path.write_bytes(_image_bytes("PNG", "blue"))
    descriptor[0]["size"] = image_path.stat().st_size
    with pytest.raises(MediaProjectionError) as changed:
        build_delivery_projection(
            descriptor,
            owner_id=owner_id,
            thread_id=thread_id,
            target=_target("native"),
            trust_descriptor_digests=True,
        )
    assert changed.value.cause == "image_digest_changed"


def test_delivery_projection_preserves_structured_non_dict_rejection(tmp_path) -> None:
    from core.media_delivery import build_delivery_projection
    from core.media_projection import MediaProjectionError

    with pytest.raises(MediaProjectionError) as rejected:
        build_delivery_projection(
            ["not-an-attachment"],
            owner_id="owner",
            thread_id="thread-id",
            target=_target("native"),
        )

    assert rejected.value.cause == "image_descriptor_invalid"


@pytest.mark.asyncio
async def test_native_queued_message_rebuilds_shared_projection(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    from PIL import Image

    from core.turn_core import BOOT_EPOCH, persist_turn_terminal, settle_turn_terminal
    from infra import config, send_service
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from registry import models
    from shared_send_fixtures import (
        _insert_session,
        _install_fake_native_context_agent,
        _thread_row,
    )

    target = _target("native")
    thread_id = "native-promoted-image"
    owner_id = "owner"
    _insert_session(
        thread_id,
        user_id=owner_id,
        model=target.catalog_id,
        spawned_by=None,
    )
    _install_fake_native_context_agent(monkeypatch)
    monkeypatch.setattr(
        send_service, "resolve_execution_target", lambda _model, **_kw: target,
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model, **_kw: target,
    )

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    image_path = (
        tmp_path / ".threads" / thread_id / "uploads" / "promoted-image" / "visual.png"
    )
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="purple").save(image_path, format="PNG")
    models.register_artifact(
        integration="upload",
        type="upload",
        external_id="promoted-image",
        parent_thread_id=thread_id,
        path=str(image_path),
    )
    attachment = {
        "file_id": "promoted-image",
        "filename": "visual.png",
        "mime_type": "image/png",
        "size": image_path.stat().st_size,
        "url": f"/api/uploads/{owner_id}/promoted-image/visual.png",
    }

    predecessor = admit_input(
        thread_id,
        role="user",
        content="old",
        intent="apply_now",
        message_id="native-image-predecessor-input",
        turn_id="native-image-predecessor",
        boot_epoch=BOOT_EPOCH,
        admitted_at="t1",
    )
    assert predecessor.batch is not None
    queued = await send_service.send_thread_input(
        thread_id,
        _thread_row(thread_id),
        "inspect the promoted image",
        user={"id": owner_id},
        attachments=[attachment],
        input_id="native-promoted-image-message",
        native_mode="event",
    )
    assert queued.receipt.message_id == "native-promoted-image-message"

    projections: list[ValidatedMediaProjection] = []

    async def accept(*_args, **kwargs):
        assert "_corsia_validated_image_sha256" not in str(
            kwargs["attachments"]
        )
        projection = kwargs["media_projection"]
        projections.append(projection)
        assert [image.attachment_id for image in projection.images] == [
            "promoted-image"
        ]
        assert projection.images[0].data == image_path.read_bytes()
        await kwargs["persist_input_once"]()
        await kwargs["settle_delivery"]()
        kwargs["start_ack"].set_result(kwargs["preadmitted_turn"])
        kwargs["provider_acceptance"].set_result(True)

    monkeypatch.setattr("ws.turn.execute_turn", accept)

    await settle_turn_terminal(predecessor.batch.turn_id, "completed")

    assert len(projections) == 1
    db = get_db()
    try:
        row = db.execute(
            "SELECT active_turn_id,delivery_state FROM messages WHERE id=?",
            (queued.receipt.message_id,),
        ).fetchone()
    finally:
        db.close()
    assert row["delivery_state"] == "delivered"
    persist_turn_terminal(str(row["active_turn_id"]), "completed")
