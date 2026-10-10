"""Fit one verified image to the one target that is about to receive it.

Separate from ``core.media_delivery`` because fitting is a property of the
target, while delivery also owns admission identity, digests, and adapter
shapes. Every image reaching a provider -- an upload or a tool media read --
passes through ``fit_image_bytes`` here and nowhere else.

A failure is always a ``MediaProjectionError``: bytes that cannot be converted
or cannot be brought under a target's limits are a decline the user can act on,
never an exception that aborts the Turn building the request.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from core.media_projection import MediaProjectionError, ValidatedImage

# A harness reaches its provider's limits through the transport it borrows, so
# the harness name is the key that resolves. Anything else is addressed by the
# provider it names directly.
IMAGE_LIMIT_PROVIDER_BY_HARNESS = {
    "claude-code": "anthropic",
    "codex": "openai",
}


def _decline(
    exc: ValueError, fallback_cause: str, actions: dict[str, str]
) -> MediaProjectionError:
    """Map one adaptation failure onto the recovery this caller can offer.

    ``core.image_resize`` raises a single error type tagged with the dead end it
    reached and deliberately names no recovery, so the same bound declines an
    upload and a tool read in each one's own terms. Anything else raising a bare
    ``ValueError`` keeps the cause its call site already established.
    """
    cause = getattr(exc, "cause", None)
    if cause not in actions:
        cause = fallback_cause
    return MediaProjectionError(cause, str(exc), actions[cause])


def target_image_limits(target: Any) -> dict | None:
    """Return the image limits one target declares, or None when unlimited.

    An unknown provider means no declared limits, which is not the same as a
    target that needs no fitting: an unsupported container is still rewritten.
    """
    from infra.config import PROVIDER_IMAGE_LIMITS

    harness = getattr(target, "harness", "") or ""
    provider = IMAGE_LIMIT_PROVIDER_BY_HARNESS.get(
        harness, getattr(target, "provider", "") or ""
    )
    return PROVIDER_IMAGE_LIMITS.get(provider)


def fit_image_bytes(
    data: bytes,
    mime_type: str,
    *,
    target: Any,
    image_count: int,
    too_large_action: str,
    unreadable_action: str,
) -> tuple[bytes, str]:
    """Fit verified image bytes to one target: container first, then size.

    The single delivery-side fitting step for uploads and tool reads alike.
    Normalizing runs even when the target declares no size limits, because a
    container the target cannot open is not a size question.

    Both decline actions come from the caller. An image that is too large and an
    image that cannot be read are different dead ends for a photo the user
    attached and a file an agent read, so only the caller knows which one the
    user can actually follow; this module never names a recovery on its own.
    """
    from core.image_resize import (
        normalize_image_for_provider,
        resize_image_for_provider,
    )

    actions = {
        "image_corrupt": unreadable_action,
        "image_too_large": too_large_action,
    }
    try:
        data, mime_type = normalize_image_for_provider(data, mime_type)
    except ValueError as exc:
        raise _decline(exc, "image_corrupt", actions) from exc
    limits = target_image_limits(target)
    if limits is None:
        return data, mime_type
    harness = getattr(target, "harness", "") or ""
    # Claude Code owns its eventual provider request and does not expose the
    # batch cardinality to CORSIA. Apply Anthropic's safe many-image dimension
    # tier so later images in that request cannot invalidate one already sent.
    threshold = limits.get("multi_image_threshold")
    if harness == "claude-code" and isinstance(threshold, int):
        image_count = max(image_count, threshold + 1)
    try:
        return resize_image_for_provider(
            data, mime_type, limits, image_count=image_count
        )
    except ValueError as exc:
        raise _decline(exc, "image_too_large", actions) from exc


def fit_validated_image(
    image: ValidatedImage, *, target: Any, image_count: int
) -> ValidatedImage:
    """Return one validated upload as the bytes this target receives.

    The admitted source stays identity; only the delivered copy is rewrapped.
    """
    data, mime_type = fit_image_bytes(
        image.data,
        image.mime_type,
        target=target,
        image_count=image_count,
        too_large_action="upload_a_smaller_image",
        unreadable_action="upload_a_valid_image",
    )
    if data == image.data and mime_type == image.mime_type:
        return image
    return ValidatedImage(
        attachment_id=image.attachment_id,
        mime_type=mime_type,
        data=data,
        sha256=sha256(data).hexdigest(),
        byte_size=len(data),
    )
