from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import seed_catalog, signed_in_default
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


def test_cc_discovery_enables_images_only_for_proven_cli_grammar() -> None:
    from connections.harness_models import normalize_claude_models

    payload = {
        "models": [{
            "value": "sonnet",
            "resolvedModel": "claude-sonnet-4-8",
        }],
    }
    older = normalize_claude_models(
        payload,
        harness_version="2.1.217",
        timestamp="2026-07-28T00:00:00+00:00",
    )
    proven = normalize_claude_models(
        payload,
        harness_version="2.1.218",
        timestamp="2026-07-28T00:00:00+00:00",
    )

    assert "input_modalities" not in older[0]["capabilities"]
    assert proven[0]["capabilities"]["input_modalities"] == ["text", "image"]


def test_proven_cc_discovery_persists_vision_target(isolated_db) -> None:
    from connections.harness_models import normalize_claude_models
    from infra.database import get_db
    from llm.resolution import resolve_execution_target

    timestamp = "2026-07-28T00:00:00+00:00"
    rows = normalize_claude_models(
        {
            "models": [{
                "value": "vision-test",
                "resolvedModel": "claude-sonnet-4-8",
            }],
        },
        harness_version="2.1.218",
        timestamp=timestamp,
    )
    db = get_db()
    try:
        seed_catalog(
            "claude-code",
            rows,
            harness_version="2.1.218",
            source="claude-stream-json:list_models",
            timestamp=timestamp,
            db=db,
        )
        target = resolve_execution_target("claude-code/vision-test", db=db)
    finally:
        db.close()

    assert target.supports_vision is True


def _target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id="claude-code/vision-test",
        harness="claude-code",
        provider="claude-code",
        backend="claude-code",
        transport_model="vision-test",
        expected_reported_model="vision-test",
        context_window=1_000_000,
        available=True,
        availability_reason=None,
        display_name="vision-test",
        supports_reasoning=True,
        supports_vision=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_subagent", [False, True])
async def test_cc_dispatch_forwards_same_projection(
    isolated_db,
    monkeypatch,
    is_subagent: bool,
) -> None:
    from infra import effort, send_dispatch, state
    from infra.send_dispatch import DispatchContext, _accept_cc

    projection = _projection()
    accepted = TurnIdentity("cc-turn", 1)
    adapter = AsyncMock(return_value=("cc-runtime", accepted))
    monkeypatch.setattr(effort, "resolve_effort", lambda *_args: None)
    signed_in_default("claude-code")
    monkeypatch.setattr(state, "owner_broadcast", lambda _owner: None)
    monkeypatch.setattr(
        send_dispatch,
        "dispatch_cc_sub" if is_subagent else "dispatch_cc_main",
        adapter,
    )
    ctx = DispatchContext(
        thread_id="cc-session",
        thread_row={
            "model": "claude-code/vision-test",
            "project_id": None,
            "user_id": "owner",
        },
        content="inspect",
        is_subagent=is_subagent,
        target=_target(),
        turn_id="cc-turn",
        media_projection=projection,
    )

    await _accept_cc(ctx)

    assert adapter.await_args.kwargs["media_projection"] is projection


@pytest.mark.asyncio
async def test_cc_runtime_cold_and_live_paths_forward_fresh_projection(monkeypatch) -> None:
    from subagents import cc_runtime as runtime
    from subagents import cc_feed, cc_feed_transition

    runtime._LIVE.reset()
    projection = _projection()
    target = _target()
    reconstructions: list[str] = []
    delivered: list[ValidatedMediaProjection] = []

    def reconstruction(*, text: str, **_kwargs):
        reconstructions.append(text)
        return SimpleNamespace(turn_text=text)

    async def resume(thread_id: str, **_kwargs):
        live = runtime._Live(
            session=SimpleNamespace(working_dir=None),
            parent_thread_id=None,
            name="cc",
            model=target.transport_model,
            catalog_model_id=target.catalog_id,
            expected_reported_model=target.expected_reported_model,
            context_window=target.context_window,
        )
        runtime._LIVE.put(thread_id, live)
        return live

    async def accept(
        _thread_id: str,
        _live,
        frame_reconstruction,
        *,
        media_projection=None,
        turn_id: str,
        **_kwargs,
    ):
        assert frame_reconstruction.turn_text in {"initial", "continuation"}
        delivered.append(media_projection)
        return TurnIdentity(turn_id, len(delivered))

    monkeypatch.setattr(cc_feed_transition, "resume_child", resume)
    monkeypatch.setattr(cc_feed_transition, "require_cc_provider_fit", lambda *_: None)
    monkeypatch.setattr(cc_feed_transition, "_proc_is_alive", lambda _session: True)
    monkeypatch.setattr(cc_feed, "build_cc_user_frame", reconstruction)
    monkeypatch.setattr(cc_feed, "accept_reconstructed_input", accept)

    try:
        first = await runtime.ensure_live_then_feed(
            "cc-cold-live",
            "initial",
            target=target,
            turn_id="turn-initial",
            record=False,
            media_projection=projection,
        )
        second = await runtime.ensure_live_then_feed(
            "cc-cold-live",
            "continuation",
            target=target,
            turn_id="turn-continuation",
            record=False,
            media_projection=projection,
        )
    finally:
        runtime._LIVE.reset()

    assert first == TurnIdentity("turn-initial", 1)
    assert second == TurnIdentity("turn-continuation", 2)
    assert reconstructions == ["initial", "continuation"]
    assert delivered == [projection, projection]


@pytest.mark.asyncio
@pytest.mark.usefixtures("signed_in_defaults")  # the queued input needs a signed-in Default: no ambient login
@pytest.mark.parametrize("mutate_before_restart", [False, True])
async def test_cc_queued_image_is_idempotent_and_revalidated_on_restart(
    isolated_db,
    monkeypatch,
    tmp_path,
    mutate_before_restart: bool,
) -> None:
    from connections import models
    from core.media_projection import MediaProjectionError
    from infra import config, send_service
    from infra.database import get_db
    from infra.input_render import project_claimed_input
    from infra.send_types import InboxQueuedInput
    from infra.thread_driver import admit_input
    from infra.thread_recovery import recover_threads
    from shared_send_fixtures import _insert_session

    thread_id = "cc-queued-image"
    owner_id = "owner"
    model_id = "claude-code/sonnet"
    _insert_session(
        thread_id,
        user_id=owner_id,
        model=model_id,
        spawned_by=None,
    )
    db = get_db()
    try:
        db.execute("UPDATE models SET supports_vision=1 WHERE id=?", (model_id,))
        db.commit()
        thread_row = dict(db.execute(
            "SELECT * FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone())
    finally:
        db.close()

    from registry import models as registry_models

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    image_path = (
        tmp_path / ".threads" / thread_id / "uploads" / "queued-image" / "visual.png"
    )
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_image_bytes("PNG", "green"))
    registry_models.register_artifact(
        integration="upload",
        type="upload",
        external_id="queued-image",
        parent_thread_id=thread_id,
        path=str(image_path),
    )
    attachment = {
        "file_id": "queued-image",
        "filename": "visual.png",
        "mime_type": "image/png",
        "size": image_path.stat().st_size,
        "url": f"/api/uploads/{owner_id}/queued-image/visual.png",
    }
    predecessor = admit_input(
        thread_id,
        role="user",
        content="old turn",
        intent="apply_now",
        message_id="cc-image-predecessor-input",
        turn_id="cc-image-predecessor",
        boot_epoch="old-boot",
        admitted_at="t1",
    )
    assert predecessor.batch is not None
    monkeypatch.setattr(models, "connection_available", lambda _name: True)

    queued = await send_service.send_thread_input(
        thread_id,
        thread_row,
        "inspect after restart",
        user={"id": owner_id},
        attachments=[attachment],
        idempotency_key="stable-image-input",
        native_mode="delegation",
        delivery_intent="queue_after_turn",
    )
    duplicate = await send_service.send_thread_input(
        thread_id,
        thread_row,
        "inspect after restart",
        user={"id": owner_id},
        attachments=[attachment],
        idempotency_key="stable-image-input",
        native_mode="delegation",
        delivery_intent="queue_after_turn",
    )

    assert isinstance(queued, InboxQueuedInput)
    assert isinstance(duplicate, InboxQueuedInput)
    assert duplicate.duplicate is True
    assert duplicate.receipt.message_id == queued.receipt.message_id
    db = get_db()
    try:
        row = db.execute(
            "SELECT metadata,delivery_options FROM messages WHERE id=?",
            (queued.receipt.message_id,),
        ).fetchone()
    finally:
        db.close()
    descriptor = json.loads(row["delivery_options"])["attachments"]
    assert descriptor[0]["_corsia_validated_image_sha256"]
    assert "_corsia_validated_image_sha256" not in str(row["metadata"])

    recovery = recover_threads(
        boot_epoch="new-boot",
        recovered_at="t2",
        successor_id=lambda _thread_id: "cc-image-successor",
    )
    assert len(recovery.settlements) == 1
    successor = recovery.settlements[0].successor
    assert successor is not None
    if mutate_before_restart:
        image_path.write_bytes(_image_bytes("PNG", "blue"))

    db = get_db()
    try:
        if mutate_before_restart:
            with pytest.raises(MediaProjectionError) as changed:
                project_claimed_input(
                    db, successor, thread_row=thread_row, target=_target(),
                )
            assert changed.value.cause == "image_digest_changed"
        else:
            projection = project_claimed_input(
                db, successor, thread_row=thread_row, target=_target(),
            ).media_projection
            assert projection.images[0].attachment_id == "queued-image"
            assert projection.images[0].data == image_path.read_bytes()
    finally:
        db.close()
