from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from shared_send_fixtures import _insert_session
from vendor_account_fixtures import signed_in_default

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


@pytest.mark.asyncio
async def test_cc_image_uses_shared_projection_without_path_prompt(
    isolated_db, monkeypatch, tmp_path
):
    from PIL import Image

    from connections import models
    from infra import config, send_service, send_preflight
    from infra.database import get_db
    from registry import models as registry_models
    from subagents import cc_runtime

    thread_id = "cc-image-typed"
    owner_id = "u-test"
    _insert_session(
        thread_id,
        user_id=owner_id,
        model="claude-code/sonnet",
        spawned_by=None,
    )
    db = get_db()
    try:
        db.execute(
            "UPDATE models SET supports_vision=1 WHERE id='claude-code/sonnet'"
        )
        db.commit()
        signed_in_default("claude-code", db=db)  # the Default lists the model with image input
        thread_row = dict(
            db.execute(
                "SELECT * FROM threads WHERE id=?",
                (thread_id,),
            ).fetchone()
        )
    finally:
        db.close()

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    image_path = (
        tmp_path / ".threads" / thread_id / "uploads" / "image-1" / "fact.png"
    )
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="purple").save(image_path, format="PNG")
    registry_models.register_artifact(
        integration="upload",
        type="upload",
        external_id="image-1",
        parent_thread_id=thread_id,
        path=str(image_path),
    )
    monkeypatch.setattr(models, "connection_available", lambda _name: True)
    monkeypatch.setattr(cc_runtime, "is_live", lambda _thread_id: False)
    from shared_send_fixtures import _checkpoint_tail_preflight
    monkeypatch.setattr(
        send_preflight,
        "context_preflight",
        _checkpoint_tail_preflight,
    )
    projections = []

    async def accept(*args, **kwargs):
        assert args[1].startswith(
            "CORSIA is delivering 2 queued inbound inputs together."
        )
        assert "[1/2]\n[PASSIVE CONTEXT DIGEST]" in args[1]
        assert args[1].endswith(
            "[2/2]\nunderstand the unique visual fact\n"
            '[To reply, call post_update(text="<your reply>")]\n'
            "If no reply is needed, do not send one."
        )
        assert str(image_path) not in args[1]
        assert "Attached files" not in args[1]
        projections.append(kwargs["media_projection"])
        await kwargs["persist_input_once"]()
        kwargs["mark_accepted"]()
        await kwargs["settle_delivery"]()
        return kwargs["preadmitted_turn"]

    provider_feed = AsyncMock(side_effect=accept)
    monkeypatch.setattr(cc_runtime, "feed_main", provider_feed)

    result = await send_service.send_thread_input(
        thread_id,
        thread_row,
        "understand the unique visual fact",
        user={"id": owner_id},
        native_mode="delegation",
        attachments=[{
            "file_id": "image-1",
            "filename": "fact.png",
            "mime_type": "image/png",
            "size": image_path.stat().st_size,
            "url": f"/api/uploads/{owner_id}/image-1/fact.png",
        }],
    )

    assert result.driver == "cc_runtime.feed_main"
    provider_feed.assert_awaited_once()
    assert len(projections) == 1
    assert [image.attachment_id for image in projections[0].images] == ["image-1"]
    assert projections[0].images[0].data == image_path.read_bytes()


@pytest.mark.asyncio
async def test_batch_accepted_effects_strip_reserved_digest(
    monkeypatch,
):
    from infra import send_effects
    from infra.thread_inputs import ClaimedBatch, ClaimedInput

    effect = AsyncMock()
    monkeypatch.setattr(send_effects, "run_send_accepted_effects", effect)
    batch = ClaimedBatch(
        thread_id="media-effects",
        turn_id="turn-1",
        turn_order=1,
        target_execution_epoch=1,
        target_model="codex/gpt-5-codex",
        items=(ClaimedInput(
            message_id="message-1",
            input_order=1,
            revision=1,
            role="user",
            content="inspect",
            intent="apply_now",
            metadata={},
            delivery_options={
                "attachments": [{
                    "file_id": "image-1",
                    "filename": "visual.png",
                    "mime_type": "image/png",
                    "_corsia_validated_image_sha256": "0" * 64,
                }],
                "accepted_effects": {"uploads": True},
            },
            created_at="t1",
        ),),
    )

    await send_effects.run_batch_accepted_effects(
        batch, {"id": "media-effects", "user_id": "owner"},
    )

    assert effect.await_args.kwargs["attachments"] == [{
        "file_id": "image-1",
        "filename": "visual.png",
        "mime_type": "image/png",
    }]
