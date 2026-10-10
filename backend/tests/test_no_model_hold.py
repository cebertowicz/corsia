"""A thread with no model keeps its inputs until the user chooses one (#2909)."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from tests.unavailable_model_support import message_row

WAITING_FOR_CHOICE = ("queued", "proved_no_write", "no_model_selected", None)
DIGEST = "context-digest:"


def _sent(ids) -> list[str]:
    """The inputs a person or event sent, without the digest riding with them."""
    return [message_id for message_id in ids if not message_id.startswith(DIGEST)]


def _model_less_thread(thread_id: str) -> dict:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?,?,'t0','t0')",
            (thread_id, "user-1"),
        )
        db.commit()
        return dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
    finally:
        db.close()


def _admit(thread_id: str, message_id: str, *, role: str = "user"):
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        return admit_input(
            thread_id, role=role, content="hello", intent="apply_now",
            message_id=message_id, turn_id=f"{message_id}-turn",
            boot_epoch="boot-1", admitted_at="t1", db=db,
        )
    finally:
        db.close()


def _column(table: str, column: str, row_id: str):
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)).fetchone()[0]
    finally:
        db.close()


def test_an_input_to_a_model_less_thread_is_held_not_rejected(isolated_db) -> None:
    _model_less_thread("t")

    admission = _admit("t", "m1")

    assert admission.batch is None
    assert admission.accepted.delivery_error == "no_model_selected"
    assert message_row("m1") == WAITING_FOR_CHOICE
    assert _column("messages", "target_model", "m1") is None


def test_a_routed_event_waits_the_same_way(isolated_db) -> None:
    _model_less_thread("t")

    _admit("t", "e1", role="system")

    assert message_row("e1") == WAITING_FOR_CHOICE


def test_a_model_less_thread_never_starts_a_turn(isolated_db) -> None:
    from infra.thread_inputs import start_idle_turn

    _model_less_thread("t")
    _admit("t", "m1")

    assert start_idle_turn(
        "t", turn_id="turn-2", boot_epoch="boot-1", started_at="t2",
    ) is None
    assert message_row("m1") == WAITING_FOR_CHOICE


@pytest.mark.asyncio
async def test_a_model_less_send_publishes_its_hold_to_every_socket(
    isolated_db, monkeypatch,
) -> None:
    """The sender's own socket must see the hold too, like the unavailable hold."""
    import infra.send_service as service
    from infra.send_types import InboxQueuedInput

    thread_row = _model_less_thread("t")
    published = Mock()
    monkeypatch.setattr("infra.state.broadcast_to_thread", published)

    result = await service.send_thread_input(
        "t", thread_row, "hello", user={"id": "user-1"},
        input_id="m1", native_mode="ws", ws=object(), broadcast_exclude_ws=object(),
    )

    assert isinstance(result, InboxQueuedInput)
    [call] = [
        c for c in published.call_args_list
        if c.args[0] == "t" and c.args[1]["message"]["id"] == "m1"
    ]
    assert call.kwargs.get("exclude_ws") is None
    assert call.args[1]["message"]["delivery_error"] == "no_model_selected"
    assert call.args[1]["message"]["write_outcome"] == "proved_no_write"


_DIGEST_KEY = "_corsia_validated_image_sha256"


def _png(color: int = 10) -> bytes:
    from io import BytesIO

    from PIL import Image

    stream = BytesIO()
    Image.new("RGB", (8, 8), (color, 0, 0)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def held_upload(isolated_db, monkeypatch, tmp_path):
    """One real image upload owned by a model-less thread, and its descriptor."""
    from infra import config
    from registry import models

    thread_row = _model_less_thread("t")
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    source = _png()
    path = tmp_path / ".threads" / "t" / "uploads" / "f1" / "a.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    models.register_artifact(
        integration="upload", type="upload", external_id="f1",
        parent_thread_id="t", path=str(path),
    )
    attachment = {
        "file_id": "f1", "filename": "a.png", "mime_type": "image/png",
        "size": len(source), "url": "/api/uploads/user-1/f1/a.png",
    }
    return thread_row, attachment, path, source


async def _send_held(thread_row, attachment, content="see attached"):
    import infra.send_service as service

    return await service.send_thread_input(
        "t", thread_row, content, user={"id": "user-1"}, input_id="m1",
        native_mode="ws", ws=object(), attachments=[attachment],
        allow_empty_content=not content,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "see attached"], ids=["attachment_only", "text_with_attachment"])
async def test_an_image_sent_to_a_model_less_thread_is_held_bound_to_its_source(
    held_upload, content,
) -> None:
    """The source is validated and digest-bound with no model resolved or preflighted."""
    import json
    from hashlib import sha256

    from infra.send_types import InboxQueuedInput

    thread_row, attachment, _path, source = held_upload

    result = await _send_held(thread_row, attachment, content)

    assert isinstance(result, InboxQueuedInput)
    assert message_row("m1") == WAITING_FOR_CHOICE
    [stored] = json.loads(_column("messages", "delivery_options", "m1"))["attachments"]
    assert stored[_DIGEST_KEY] == sha256(source).hexdigest()


def _rewrite_declared_size(attachment, _path):
    attachment["size"] += 1


def _delete_upload(_attachment, path):
    path.unlink()


def _corrupt_upload(_attachment, path):
    path.write_bytes(b"not an image")


def _unknown_upload(attachment, _path):
    attachment["file_id"] = "missing"
    attachment["url"] = "/api/uploads/user-1/missing/a.png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spoil", "cause"),
    [
        (_rewrite_declared_size, "image_size_changed"),
        (_delete_upload, "image_not_found"),
        (_corrupt_upload, "image_size_changed"),
        (_unknown_upload, "image_not_found"),
    ],
    ids=["size_changed", "file_missing", "bytes_replaced", "upload_unknown"],
)
async def test_an_invalid_image_is_rejected_at_admission_not_held(
    held_upload, spoil, cause,
) -> None:
    """A model-less thread is no looser than a modelled one about the source."""
    from infra.database import get_db
    from infra.send_types import SendRejected

    thread_row, attachment, path, _source = held_upload
    spoil(attachment, path)

    with pytest.raises(SendRejected) as rejected:
        await _send_held(thread_row, attachment)

    assert rejected.value.metadata["cause"] == cause
    db = get_db()
    try:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE id='m1'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_held_image_delivers_after_the_first_model_with_its_identity_intact(
    held_upload, monkeypatch,
) -> None:
    import json
    from hashlib import sha256
    from types import SimpleNamespace

    import infra.thread_execution as execution
    from core.media_delivery import build_delivery_projection
    from core.media_projection import MediaProjectionError
    from thread.model import switch_model

    monkeypatch.setattr(execution, "drive_thread", AsyncMock())
    thread_row, attachment, path, source = held_upload
    await _send_held(thread_row, attachment)
    await switch_model(
        "t", "claude-code/sonnet", user_id="user-1", actor_kind="user",
        actor_id="user-1", reason="harness_model_switch",
    )
    assert message_row("m1") == ("queued", None, None, None)
    [stored] = json.loads(_column("messages", "delivery_options", "m1"))["attachments"]
    target = SimpleNamespace(
        catalog_id="claude-code/sonnet", harness="claude-code", provider="unused",
        supports_vision=True,
    )

    def deliver():
        return build_delivery_projection(
            [stored], owner_id="user-1", thread_id="t", target=target,
            trust_descriptor_digests=True,
        )

    _public, projection = deliver()
    assert projection.digests == (sha256(source).hexdigest(),)

    from core.media_delivery import build_delivery_projection_async

    with pytest.raises(ValueError, match="requires a resolved target"):
        await build_delivery_projection_async(
            [stored], owner_id="user-1", thread_id="t", target=None,
            trust_descriptor_digests=True,
        )

    path.write_bytes(_png(200))  # swapped while it waited for a model
    with pytest.raises(MediaProjectionError) as changed:
        deliver()
    assert changed.value.cause == "image_digest_changed"


@pytest.mark.asyncio
async def test_choosing_a_first_model_releases_the_held_inputs_in_order(
    isolated_db, monkeypatch,
) -> None:
    """The user's choice retargets what waited, and wakes the thread."""
    import infra.thread_execution as execution
    from thread.model import switch_model

    drive = AsyncMock()
    monkeypatch.setattr(execution, "drive_thread", drive)
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    _model_less_thread("t")
    _admit("t", "m1")
    _admit("t", "m2")

    outcome = await switch_model(
        "t", "claude-code/sonnet", user_id="user-1", actor_kind="user",
        actor_id="user-1", reason="harness_model_switch",
    )

    assert outcome.changed is True
    assert outcome.old_model is None
    assert outcome.new_model == "claude-code/sonnet"
    assert _column("threads", "model", "t") == "claude-code/sonnet"
    for message_id in ("m1", "m2"):
        assert message_row(message_id) == ("queued", None, None, None)
        assert _column("messages", "target_model", message_id) == "claude-code/sonnet"
    drive.assert_awaited_once_with("t")


@pytest.mark.asyncio
async def test_choosing_a_model_that_cannot_run_keeps_the_input_waiting(
    isolated_db, monkeypatch,
) -> None:
    """A model that cannot run is refused, so the thread still waits for a real choice."""
    import infra.thread_execution as execution
    from thread.model import switch_model
    from thread.model_state import ModelSwitchPreparationError

    monkeypatch.setattr(execution, "drive_thread", AsyncMock())
    _model_less_thread("t")
    _admit("t", "m1")

    with pytest.raises(ModelSwitchPreparationError):
        await switch_model(
            "t", "nope/missing", user_id="user-1", actor_kind="user",
            actor_id="user-1", reason="harness_model_switch",
        )

    assert _column("threads", "model", "t") is None
    assert message_row("m1") == WAITING_FOR_CHOICE
