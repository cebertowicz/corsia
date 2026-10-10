"""The real Native FIFO adapter carries validated uploads to the provider frame."""

from io import BytesIO

import pytest
from PIL import Image


@pytest.mark.asyncio
@pytest.mark.parametrize("counting_model", ["anthropic/test", "openai/gpt-4"])
async def test_native_safe_boundary_preserves_each_members_uploaded_image(
    isolated_db, monkeypatch, tmp_path, counting_model,
):
    from infra import config
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from registry import models
    from test_native_inbox_batch import _seed, _thread_row
    from ws.native_inbox_batch import native_safe_boundary_input

    thread_id = "native-boundary-media"
    identity = _seed(thread_id)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    with get_db() as db:
        db.execute("UPDATE models SET supports_vision=1,litellm_model=? WHERE id='native/test'", (counting_model,))
        db.commit()
    for index, color in enumerate(("red", "blue")):
        image_id = f"boundary-image-{index}"
        stream = BytesIO()
        Image.new("RGB", (2, 2), color).save(stream, format="PNG")
        data = stream.getvalue()
        path = tmp_path / ".threads" / thread_id / "uploads" / image_id / "image.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        models.register_artifact(
            integration="upload", type="upload", external_id=image_id,
            parent_thread_id=thread_id, path=str(path),
        )
        accept_thread_input(
            thread_id, role="user", content=f"inspect-{index}", intent="apply_now",
            message_id=f"media-input-{index}", delivery_options={"attachments": [{
                "file_id": image_id, "filename": "image.png", "mime_type": "image/png",
                "size": len(data), "url": f"/api/uploads/u-test/{image_id}/image.png",
            }]},
        )

    candidate = await native_safe_boundary_input(
        thread_id=thread_id, thread_row=_thread_row(thread_id),
        owner_turn_id=identity.turn_id, max_input_tokens=100_000,
    )
    assert candidate is not None
    assert isinstance(candidate.content, list)
    assert [block["type"] for block in candidate.content] == ["image_url", "image_url", "text"]
    images = [block["image_url"]["url"] for block in candidate.content[:2]]
    assert images[0] != images[1]
    assert all(image.startswith("data:image/png;base64,") for image in images)
    text = candidate.content[-1]["text"]
    assert text.index("inspect-0") < text.index("inspect-1")
    from core.destination_preflight import attach_native_provider_fit_ledger

    prepared = attach_native_provider_fit_ledger(
        target_catalog_id="native/test", proven_target_window_tokens=100_000,
        messages=[{"role": "user", "content": candidate.content}], tools=[],
        current_input=candidate.content, input_identity=candidate.input_identity,
        model=counting_model,
    )
    assert prepared.fit_ledger.fits
    assert prepared.fit_ledger.rendered_input_tokens >= 2_000
    await candidate.before_provider_write()
    await candidate.settle_delivery()
    with get_db() as db:
        rows = db.execute(
            "SELECT id,delivery_state,write_outcome,seq FROM messages "
            "WHERE id IN ('media-input-0','media-input-1') ORDER BY input_order",
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("media-input-0", "delivered", "accepted", 2),
        ("media-input-1", "delivered", "accepted", 3),
    ]


@pytest.mark.asyncio
async def test_failed_boundary_projection_releases_unwritten_claim(isolated_db, monkeypatch):
    from infra.database import get_db
    from test_native_inbox_batch import _seed, _enqueue, _thread_row
    from ws.native_inbox_batch import native_safe_boundary_input

    thread_id = "native-boundary-invalid-media"
    identity = _seed(thread_id)
    _enqueue(thread_id, "unwritten-media", "inspect")

    def reject(*args, **kwargs):
        raise ValueError("invalid upload")

    monkeypatch.setattr("infra.input_render.project_claimed_input", reject)
    with pytest.raises(ValueError, match="invalid upload"):
        await native_safe_boundary_input(
            thread_id=thread_id, thread_row=_thread_row(thread_id),
            owner_turn_id=identity.turn_id, max_input_tokens=100_000,
        )
    with get_db() as db:
        row = db.execute(
            "SELECT active_turn_id,delivery_state,delivery_attempt_id,seq "
            "FROM messages WHERE id='unwritten-media'",
        ).fetchone()
    assert tuple(row) == (None, "queued", None, None)
