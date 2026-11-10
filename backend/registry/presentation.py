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

from registry.provider_continuity import HARNESS_CONTINUITY_TYPES

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
    ("upload", "image"): PresentationDescriptor(
        label="Image", icon="image", capability=CAPABILITY_LEAF_PACKAGE,
    ),
    ("upload", "upload"): PresentationDescriptor(
        label="Upload", icon="paperclip", capability=CAPABILITY_DOWNLOAD,
    ),
    ("github", "pull-request"): PresentationDescriptor(
        label="Pull Request", icon="git-pull-request", capability=CAPABILITY_EXTERNAL_LINK,
    ),
}

#: Identities that are never pane rows: implementation continuation handles,
#: plan decisions, which are answered in their chip and kept on their plan
#: node (#1820), and the per-thread routing identities the Gmail binder
#: registers for delivered mail. Kept alongside the descriptor table because
#: both encode "what a type is", not pane-projection control flow.
NON_PANE_TYPES: frozenset[tuple[str, str]] = frozenset({
    *HARNESS_CONTINUITY_TYPES.values(),
    ("gmail", "email-thread"),
    ("local", "plan-decision"),
})


#: The folder the draft tools write email drafts into (tools/google_email.py and tools/microsoft_actions.py import it).
#: An email draft is a workspace document in that folder; the descriptor below is the one rule that says so.
EMAIL_DRAFT_FOLDER = "email-drafts"

_EMAIL_DRAFT = PresentationDescriptor(
    label="Email draft", icon="mail", capability=CAPABILITY_LEAF_PACKAGE,
)


def resolve_descriptor(integration: str, type_: str, path: str | None = None) -> PresentationDescriptor:
    """Return the trusted descriptor for an artifact's integration and type.

    Never raises: an unregistered type renders as the honest unsupported
    state rather than breaking the pane. An email draft (a workspace document in the draft folder) gets the
    envelope, here and nowhere else.
    """
    if (integration, type_) == ("workspace", "document") and path and Path(path).parent.name == EMAIL_DRAFT_FOLDER:
        return _EMAIL_DRAFT
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
