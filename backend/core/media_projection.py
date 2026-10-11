"""Owner-scoped validation of fresh image attachments from authenticated uploads."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from infra.config import MAX_IMAGE_UPLOAD_BYTES, VISION_MIME_TYPES

_DEFAULT_MAX_IMAGE_COUNT = 20
_DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_IMAGE_MIME_ALIASES = {"image/jpg": "image/jpeg"}


class MediaProjectionError(ValueError):
    """Safe structured rejection from the fresh-image projection boundary."""

    def __init__(
        self,
        cause: str,
        detail: str,
        legal_action: str,
    ) -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail
        self.legal_action = legal_action


@dataclass(frozen=True, slots=True)
class MediaProjectionLimits:
    """Request-wide source-byte limits applied before adapter translation."""

    max_count: int = _DEFAULT_MAX_IMAGE_COUNT
    max_image_bytes: int = MAX_IMAGE_UPLOAD_BYTES
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        if min(self.max_count, self.max_image_bytes, self.max_total_bytes) < 1:
            raise ValueError("media projection limits must be positive")


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    """One ordered image whose owner, bytes, MIME, and digest are proven."""

    attachment_id: str
    mime_type: str
    data: bytes
    sha256: str
    byte_size: int

    def data_url(self) -> str:
        """Render the validated bytes as a provider-safe data URL.

        Raises:
            ValueError: If these bytes were never fitted to a provider container.
                Reaching this means a delivery path skipped
                ``core.media_delivery``, so a target would receive a container it
                cannot open and fail with an error the user cannot act on.
        """

        if self.mime_type not in VISION_MIME_TYPES:
            raise ValueError(
                f"{self.mime_type} must be fitted by core.media_delivery before "
                "it is delivered to a provider"
            )
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime_type};base64,{encoded}"


@dataclass(frozen=True, slots=True)
class ValidatedMediaProjection:
    """Ordered images a target receives, each with its admitted source digest.

    Delivery may fit ``images`` to a provider; ``source_digests`` never change.
    """

    images: tuple[ValidatedImage, ...]
    source_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.source_digests) != len(self.images):
            raise ValueError("every projected image needs its source digest")

    @property
    def digests(self) -> tuple[str, ...]:
        """Return admitted source digests in attachment order."""

        return self.source_digests


def _reject(cause: str, detail: str, legal_action: str) -> MediaProjectionError:
    return MediaProjectionError(cause, detail, legal_action)


def _canonical_image_mime(mime_type: Any) -> Any:
    return _IMAGE_MIME_ALIASES.get(mime_type, mime_type) if isinstance(mime_type, str) else mime_type

def _safe_component(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return None
    path = Path(value)
    if path.is_absolute() or path.name != value or "/" in value or "\\" in value:
        return None
    return value


def _partition_attachments(
    attachments: Sequence[dict],
) -> tuple[list[dict], list[dict]]:
    images: list[dict] = []
    upload_candidates: list[dict] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise _reject(
                "image_descriptor_invalid",
                "An image attachment descriptor is invalid.",
                "remove_and_upload_the_image_again",
            )
        mime_type = _canonical_image_mime(attachment.get("mime_type"))
        if isinstance(mime_type, str) and mime_type.startswith("image/"):
            # Any declared image is an image candidate. Which containers a
            # provider can open is decided from the bytes, once, at delivery
            # (core.media_delivery); refusing a photo because its container is
            # HEIC or TIFF is a dead end the user cannot act on.
            images.append(attachment)
        elif attachment.get("file_id") and attachment.get("filename"):
            upload_candidates.append(attachment)
    return images, upload_candidates


def _preflight_target(target: Any) -> None:
    if not bool(getattr(target, "supports_vision", False)):
        raise _reject(
            "image_model_unsupported",
            "The selected model cannot accept image input.",
            "choose_a_vision_capable_model_or_send_without_images",
        )


def _validate_upload_url(
    raw_url: Any,
    *,
    owner_id: str,
    file_id: str,
    filename: str,
) -> None:
    if raw_url in (None, ""):
        return
    if not isinstance(raw_url, str):
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference is invalid.",
            "remove_and_upload_the_image_again",
        )
    parsed = urlsplit(raw_url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference is invalid.",
            "remove_and_upload_the_image_again",
        )
    if len(parts) != 5 or parts[:2] != ["api", "uploads"]:
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference is invalid.",
            "remove_and_upload_the_image_again",
        )
    if parts[2] != owner_id:
        raise _reject(
            "image_owner_mismatch",
            "The image attachment is not owned by this thread's user.",
            "upload_the_image_as_the_thread_owner",
        )
    if parts[3:] != [file_id, filename]:
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference does not match its attachment identity.",
            "remove_and_upload_the_image_again",
        )


def _thread_upload_root(*, thread_id: str, owner_id: str) -> Path:
    """Resolve the caller-owned thread's durable upload root or reject it."""
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_uploads_root,
        resolve_workspace_root,
    )
    from infra import config
    from infra.database import get_db
    from infra.queries import get_thread_for_user

    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, owner_id, db=db)
        if thread_row is None:
            raise _reject(
                "image_owner_mismatch",
                "The image attachment is not owned by this thread's user.",
                "upload_the_image_as_the_thread_owner",
            )
        project_id = thread_row["project_id"]
        scope = (
            resolve_project_root(config.WORKSPACE, project_id, db=db)
            if project_id
            else resolve_workspace_root(config.WORKSPACE)
        )
        if scope is None:
            raise _reject(
                "image_not_found",
                "The authenticated image upload is no longer available.",
                "upload_the_image_again",
            )
        return resolve_thread_uploads_root(scope, thread_id).resolve(strict=False)
    except MediaProjectionError:
        raise
    except (OSError, ValueError):
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference is invalid.",
            "remove_and_upload_the_image_again",
        ) from None
    finally:
        db.close()


def _resolve_upload(
    attachment: dict,
    *,
    owner_id: str,
    thread_id: str,
    thread_upload_root: Path,
) -> tuple[str, Path]:
    from registry import models

    file_id = _safe_component(attachment.get("file_id"))
    filename = _safe_component(attachment.get("filename"))
    if (
        file_id is None
        or filename is None
        or _safe_component(owner_id) is None
        or _safe_component(thread_id) is None
    ):
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference is invalid.",
            "remove_and_upload_the_image_again",
        )
    _validate_upload_url(
        attachment.get("url"),
        owner_id=owner_id,
        file_id=file_id,
        filename=filename,
    )
    artifact = models.resolve_upload(file_id)
    if artifact is None or not isinstance(artifact.get("path"), str):
        raise _reject(
            "image_not_found",
            "The authenticated image upload is no longer available.",
            "upload_the_image_again",
        )
    if artifact.get("parent_thread_id") != thread_id:
        raise _reject(
            "image_owner_mismatch",
            "The image attachment is not owned by this thread's user.",
            "upload_the_image_as_the_thread_owner",
        )
    try:
        resolved = Path(artifact["path"]).resolve(strict=False)
    except (OSError, ValueError, TypeError):
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference is invalid.",
            "remove_and_upload_the_image_again",
        ) from None
    if (
        resolved.name != filename
        or not resolved.is_relative_to(thread_upload_root)
    ):
        raise _reject(
            "image_reference_unsafe",
            "The image upload reference escapes its thread scope.",
            "remove_and_upload_the_image_again",
        )
    if not resolved.is_file():
        raise _reject(
            "image_not_found",
            "The authenticated image upload is no longer available.",
            "upload_the_image_again",
        )
    return file_id, resolved


def build_media_projection(
    attachments: Sequence[dict] | None,
    *,
    owner_id: str,
    thread_id: str,
    target: Any,
    limits: MediaProjectionLimits | None = None,
    expected_digests: Sequence[str] | None = None,
) -> ValidatedMediaProjection:
    """Validate owner-scoped fresh images and preserve their exact input order."""

    images, upload_candidates = _partition_attachments(
        list(attachments or ())
    )
    policy = limits or MediaProjectionLimits()
    thread_upload_root = None
    if upload_candidates:
        thread_upload_root = _thread_upload_root(
            thread_id=thread_id,
            owner_id=owner_id,
        )
        from core.media_image_validation import reject_misdeclared_images

        reject_misdeclared_images(
            upload_candidates,
            owner_id=owner_id,
            thread_id=thread_id,
            thread_upload_root=thread_upload_root,
        )
    if not images:
        return ValidatedMediaProjection((), ())
    if target is not None:
        _preflight_target(target)
    if thread_upload_root is None:
        thread_upload_root = _thread_upload_root(
            thread_id=thread_id,
            owner_id=owner_id,
        )
    if len(images) > policy.max_count:
        raise _reject(
            "image_count_exceeded",
            "Too many images were attached to one message.",
            "send_fewer_images",
        )
    if expected_digests is not None and len(expected_digests) != len(images):
        raise _reject(
            "image_digest_changed",
            "The admitted image identity no longer matches this delivery.",
            "upload_the_images_again",
        )
    from core.media_image_validation import read_validated_image

    projected: list[ValidatedImage] = []
    total_bytes = 0
    for index, attachment in enumerate(images):
        expected = expected_digests[index] if expected_digests is not None else None
        image = read_validated_image(
            attachment,
            owner_id=owner_id,
            thread_id=thread_id,
            thread_upload_root=thread_upload_root,
            max_image_bytes=policy.max_image_bytes,
            expected_digest=expected,
        )
        total_bytes += image.byte_size
        if total_bytes > policy.max_total_bytes:
            raise _reject(
                "image_total_too_large",
                "The attached images exceed the total byte limit.",
                "send_fewer_or_smaller_images",
            )
        projected.append(image)
    sources = tuple(projected)
    return ValidatedMediaProjection(sources, tuple(i.sha256 for i in sources))
