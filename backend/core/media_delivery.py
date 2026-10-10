"""Carry validated fresh images from admission to provider adapters."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from core.media_fit import fit_image_bytes, fit_validated_image
from core.media_projection import (
    MediaProjectionError,
    ValidatedImage,
    ValidatedMediaProjection,
    build_media_projection,
)

_DIGEST_KEY = "_corsia_validated_image_sha256"

# Wire discriminator for the Claude Code loopback bridge. The MCP shim runs in
# the Claude Code subprocess and cannot import backend modules, so it matches
# this literal (pinned equal by test) after authenticating the envelope.
_CC_MEDIA_KIND = "corsia_tool_media"

def public_attachments(attachments: Sequence[dict] | None) -> list:
    """Strip private fields while preserving invalid items for canonical rejection."""

    return [
        {key: value for key, value in item.items() if key != _DIGEST_KEY}
        if isinstance(item, dict)
        else item
        for item in (attachments or ())
    ]


def expected_image_digests(
    attachments: Sequence[dict] | None,
) -> tuple[str, ...] | None:
    """Read complete admitted image digests, or allow a legacy unbound descriptor."""

    if any(not isinstance(item, dict) for item in (attachments or ())):
        raise MediaProjectionError(
            "image_descriptor_invalid",
            "An image attachment descriptor is invalid.",
            "remove_and_upload_the_image_again",
        )
    image_items = [
        item
        for item in (attachments or ())
        if isinstance(item.get("mime_type"), str)
        and item["mime_type"].startswith("image/")
    ]
    if not image_items:
        return None
    values = [item.get(_DIGEST_KEY) for item in image_items]
    if all(value is None for value in values):
        return None
    digests: list[str] = []
    for value in values:
        if not (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        ):
            raise MediaProjectionError(
                "image_digest_changed",
                "The admitted image identity no longer matches this delivery.",
                "upload_the_images_again",
            )
        digests.append(value)
    return tuple(digests)


def bind_projection_to_descriptor(
    attachments: Sequence[dict] | None,
    projection: ValidatedMediaProjection,
) -> list[dict]:
    """Copy public metadata and bind each image to its admitted source digest."""

    copied = public_attachments(attachments)
    projected = zip(projection.images, projection.digests)
    for item in copied:
        mime_type = item.get("mime_type")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            continue
        try:
            image, source_digest = next(projected)
        except StopIteration as exc:
            raise ValueError("projection does not cover every image attachment") from exc
        if image.attachment_id != item.get("file_id"):
            raise ValueError("projection image order does not match attachments")
        item[_DIGEST_KEY] = source_digest
    try:
        next(projected)
    except StopIteration:
        return copied
    raise ValueError("projection contains an image without an attachment")


def _fit_projection_images(
    source: ValidatedMediaProjection, *, target: Any
) -> ValidatedMediaProjection:
    """Fit every validated source image to one target, keeping its digest."""
    count = len(source.images)
    fitted = tuple(
        fit_validated_image(image, target=target, image_count=count)
        for image in source.images
    )
    return ValidatedMediaProjection(fitted, source.digests)


def build_delivery_projection(
    attachments: Sequence[dict] | None,
    *,
    owner_id: str,
    thread_id: str,
    target: Any,
    trust_descriptor_digests: bool = False,
) -> tuple[list[dict], ValidatedMediaProjection]:
    """Validate uploads as unchanged sources, then fit them to this target.

    Digest fields are trusted only on a server-owned descriptor. Admission and
    every later delivery call this with their own target, so the durable source
    is never rewritten and a model switch re-fits from full resolution.
    """

    expected = (
        expected_image_digests(attachments)
        if trust_descriptor_digests
        else None
    )
    public = public_attachments(attachments)
    source = build_media_projection(
        public,
        owner_id=owner_id,
        thread_id=thread_id,
        target=target,
        expected_digests=expected,
    )
    return public, _fit_projection_images(source, target=target)


async def build_delivery_projection_async(
    attachments: Sequence[dict] | None,
    *,
    owner_id: str,
    thread_id: str,
    target: Any,
    trust_descriptor_digests: bool = False,
) -> tuple[list[dict], ValidatedMediaProjection]:
    """Validate on the shared DB pool; fit CPU-bound image bytes off it.

    Fitting decodes and re-encodes full images behind a small concurrency
    semaphore (``core.image_resize``'s ``_NORMALIZATION_SLOTS``) whose acquire
    blocks the calling thread until a slot frees. Running that wait on
    ``infra.db_async``'s shared pool would hold one of its few workers hostage
    for the wait, starving every unrelated database operation behind it.
    Validation still needs the DB pool for its registry/file lookups.

    ``target=None`` (a thread with no model yet) validates and digest-binds the
    sources with no capability preflight or fit; delivery fits them to the model
    the user later chooses. Delivery (``trust_descriptor_digests``) always has a
    target, so a missing one there is a bug, never a silent unfitted send.
    """
    from infra.db_async import run_in_db

    if target is None and trust_descriptor_digests:
        raise ValueError("delivering an image requires a resolved target")

    expected = (
        expected_image_digests(attachments)
        if trust_descriptor_digests
        else None
    )
    public = public_attachments(attachments)
    source = await run_in_db(
        build_media_projection,
        public,
        owner_id=owner_id,
        thread_id=thread_id,
        target=target,
        expected_digests=expected,
    )
    if not source.images or target is None:
        return public, source
    projection = await asyncio.to_thread(_fit_projection_images, source, target=target)
    return public, projection


def codex_input_items(
    text: str,
    projection: ValidatedMediaProjection | None,
) -> list[dict[str, Any]]:
    """Translate one projection to Codex app-server's proven ordered grammar."""

    images = projection.images if projection is not None else ()
    return [
        *({"type": "image", "url": image.data_url()} for image in images),
        {"type": "text", "text": text, "text_elements": []},
    ]


@dataclass(frozen=True, slots=True)
class ToolMediaContent:
    """What one target may actually receive for a media tool result."""

    text: str
    image: ValidatedImage | None


def _model_name(target: Any) -> str:
    for attribute in ("display_name", "catalog_id", "transport_model"):
        value = getattr(target, attribute, None)
        if isinstance(value, str) and value:
            return value
    return "the active model"


def _decline(text: str) -> ToolMediaContent:
    return ToolMediaContent(text=text, image=None)


def _materialize_image(
    reference: Any,
    *,
    target: Any,
    image_count: int = 1,
) -> ValidatedImage:
    """Resolve one referenced image into validated, provider-sized bytes."""
    from core.media_image_validation import detect_and_verify_image
    from tools.filesystem_binary import open_bound_binary

    with open_bound_binary(Path(reference.path)) as (handle, file_stat):
        if file_stat.st_size != reference.byte_size:
            raise MediaProjectionError(
                "image_size_changed",
                "The file changed after this read was recorded.",
                "read_the_file_again",
            )
        data = handle.read(reference.byte_size)
    if sha256(data).hexdigest() != reference.sha256:
        raise MediaProjectionError(
            "image_digest_changed",
            "The file changed after this read was recorded.",
            "read_the_file_again",
        )
    mime_type = detect_and_verify_image(data, reference.mime_type)
    data, mime_type = fit_image_bytes(
        data,
        mime_type,
        target=target,
        image_count=image_count,
        too_large_action="read_a_smaller_image",
        unreadable_action="read_the_file_again",
    )
    return ValidatedImage(
        attachment_id=reference.sha256,
        mime_type=mime_type,
        data=data,
        sha256=sha256(data).hexdigest(),
        byte_size=len(data),
    )


def tool_result_media_content(
    text: str,
    reference: Any,
    *,
    target: Any,
    image_count: int = 1,
) -> ToolMediaContent:
    """Decide once what a target receives for one referenced media read.

    This is the sole place capability, limits, and byte materialization are
    resolved; the native, Claude Code, and Codex renderers stay translation-only.
    """
    from core.media_reads import IMAGE_KIND, VIDEO_KIND

    name = reference.filename
    if reference.kind == VIDEO_KIND:
        return _decline(
            f"{text} The active model {_model_name(target)} cannot accept video "
            "input through CORSIA's current provider adapter, so "
            f"{name} was not sent. Supply a representative image, or describe "
            "what you need from the video."
        )
    if reference.kind != IMAGE_KIND:
        return _decline(text)
    if not bool(getattr(target, "supports_vision", False)):
        return _decline(
            f"{text} The active model {_model_name(target)} cannot accept image "
            f"input, so {name} was not sent. Switch to a vision-capable model to "
            "look at it."
        )
    try:
        image = _materialize_image(
            reference, target=target, image_count=image_count
        )
    except MediaProjectionError as exc:
        return _decline(f"{text} {name} could not be delivered: {exc.detail}")
    except OSError:
        return _decline(
            f"{text} {name} is no longer readable at the path this result "
            "recorded."
        )
    return ToolMediaContent(text=text, image=image)


def tool_result_media_for_reference(
    text: str,
    reference: Any,
    model: str | None,
    *,
    image_count: int = 1,
) -> ToolMediaContent:
    """Resolve one admitted media reference for the exact active model."""
    from infra.database import get_db
    from llm.resolution import resolve_execution_target

    db = get_db()
    try:
        target = resolve_execution_target(model, db=db)
    finally:
        db.close()
    return tool_result_media_content(
        text, reference, target=target, image_count=image_count
    )


def tool_result_media_for_model(
    delivery: Any,
    model: str | None,
    *,
    image_count: int = 1,
) -> ToolMediaContent | None:
    """Resolve one canonical media result for the exact active model."""
    from core.media_reads import media_reference_from_metadata

    reference = media_reference_from_metadata(
        getattr(delivery, "media_metadata", None)
    )
    if reference is None:
        return None
    return tool_result_media_for_reference(
        delivery.content, reference, model, image_count=image_count
    )


def codex_tool_content_items(
    content: ToolMediaContent | None,
    *,
    text: str | None = None,
) -> list[dict[str, Any]]:
    """Render one tool result in Codex's DynamicToolCallResponse grammar."""

    if content is None:
        return [{"type": "inputText", "text": text or ""}]
    image_items = (
        [{"type": "inputImage", "imageUrl": content.image.data_url()}]
        if content.image is not None
        else []
    )
    return [*image_items, {"type": "inputText", "text": content.text}]


def cc_tool_bridge_payload(content: ToolMediaContent, *, secret: str) -> str:
    """Serialize a token-authenticated media result for Claude Code's bridge."""

    payload: dict[str, Any] = {"kind": _CC_MEDIA_KIND, "text": content.text}
    if content.image is not None:
        payload["image"] = {
            "mime_type": content.image.mime_type,
            "data": base64.b64encode(content.image.data).decode("ascii"),
        }
    unsigned = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["signature"] = hmac.new(
        secret.encode("utf-8"),
        unsigned.encode("utf-8"),
        sha256,
    ).hexdigest()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
