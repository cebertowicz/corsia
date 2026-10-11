"""Trusted artifact presentation registry.

Resolves a stable label/icon/capability descriptor by ``(integration, type)``
for the pane and Artifact Leaf. This is new internal infra — deliberately not
``tools/catalog.py``, which resolves harness-tool schemas, not artifact
presentation. Core ships handlers for the types this backend actually
produces (workspace document/code/image/skill, upload, GitHub pull request); any
other type falls back to an honest unsupported descriptor rather than a
silently-broken row. A connector contributes a new descriptor by registering
one here, never by branching pane control flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from registry.identity_metadata import parse_metadata

CAPABILITY_LEAF_PACKAGE = "leaf-package"
CAPABILITY_EXTERNAL_LINK = "external-link"
CAPABILITY_DOWNLOAD = "download"
CAPABILITY_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PresentationDescriptor:
    """A resolved, backend-owned presentation for one artifact type."""

    label: str
    icon: str
    capability: str


_UNSUPPORTED = PresentationDescriptor(
    label="Unsupported",
    icon="file-question",
    capability=CAPABILITY_UNSUPPORTED,
)

_DESCRIPTORS: dict[tuple[str, str], PresentationDescriptor] = {
    ("workspace", "document"): PresentationDescriptor(
        label="Document", icon="file-text", capability=CAPABILITY_LEAF_PACKAGE,
    ),
    ("workspace", "code"): PresentationDescriptor(
        label="Code", icon="file-code", capability=CAPABILITY_LEAF_PACKAGE,
    ),
    ("workspace", "image"): PresentationDescriptor(
        label="Image", icon="image", capability=CAPABILITY_LEAF_PACKAGE,
    ),
    ("workspace", "skill"): PresentationDescriptor(
        label="Skill", icon="sparkles", capability=CAPABILITY_LEAF_PACKAGE,
    ),
    ("upload", "upload"): PresentationDescriptor(
        label="Upload", icon="paperclip", capability=CAPABILITY_DOWNLOAD,
    ),
    ("github", "pull-request"): PresentationDescriptor(
        label="Pull Request", icon="git-pull-request", capability=CAPABILITY_EXTERNAL_LINK,
    ),
}

#: Identities that are never pane rows: implementation continuation handles,
#: and plan decisions, which are answered in their chip and kept on their plan
#: node (#1820). Kept alongside the descriptor table because both encode "what
#: a type is", not pane-projection control flow.
NON_PANE_TYPES: frozenset[tuple[str, str]] = frozenset({
    ("claude-code", "session"),
    ("codex", "thread"),
    ("local", "plan-decision"),
})


_UPLOAD_IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})
_UPLOAD_IMAGE_SUFFIXES = frozenset({".png", ".jpeg", ".jpg", ".gif", ".webp"})


def _is_existing_local_image_upload(
    path: object, metadata: object, title: object,
) -> bool:
    """Whether this upload can use the canvas image leaf safely.

    Existing upload registrations retain ``type=upload``. Their descriptor
    promotes only the concrete local files the leaf can open, preserving the
    download behavior for remote, missing, or non-image upload records.
    """
    if not isinstance(path, str):
        return False
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        return False
    meta = metadata if isinstance(metadata, Mapping) else parse_metadata(metadata)
    mime = meta.get("mime")
    if isinstance(mime, str) and mime.lower() in _UPLOAD_IMAGE_MIME_TYPES:
        return True
    if mime:
        return False
    filename = title if isinstance(title, str) else candidate.name
    return Path(filename).suffix.lower() in _UPLOAD_IMAGE_SUFFIXES


def resolve_descriptor(
    integration: str,
    type_: str,
    *,
    path: object = None,
    metadata: object = None,
    title: object = None,
) -> PresentationDescriptor:
    """Return the trusted descriptor for an artifact's presentation inputs.

    Never raises: an unregistered type renders as the honest unsupported
    state rather than breaking the pane.
    """
    if (
        integration == "upload"
        and type_ == "upload"
        and _is_existing_local_image_upload(path, metadata, title)
    ):
        return _DESCRIPTORS[("workspace", "image")]
    return _DESCRIPTORS.get((integration, type_), _UNSUPPORTED)


def register_descriptor(
    integration: str, type_: str, descriptor: PresentationDescriptor
) -> None:
    """Register a trusted descriptor for a connector-contributed type.

    Core handlers above are registered at import time; a connector calls
    this once at load time to add its own type without editing pane control
    flow. Not exposed to untrusted/dynamically-loaded code (design non-goal).
    """
    _DESCRIPTORS[(integration, type_)] = descriptor
