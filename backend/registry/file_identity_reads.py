"""Read routing for file identities and their current owners."""

from __future__ import annotations

from core.workspace_layout import uploads_root_for_path
from registry import models, registration_events
from registry.file_identity import FileIdentity


def find_workspace(identity: FileIdentity, *, db):
    """Find a workspace identity in any state, logical before legacy."""
    return models.find_identity_by_external_ids(
        "workspace", identity.artifact_type,
        (identity.external_id, identity.canonical), db=db,
    )


def _read(identity: FileIdentity, *, include_retired: bool, db):
    """Route active owners first; inspect any-state identities only on a miss."""
    for candidate in dict.fromkeys((identity.external_id, identity.canonical)):
        artifact = models.resolve("workspace", identity.artifact_type, candidate, db=db)
        if artifact is not None:
            return artifact, True
    upload_eligible = uploads_root_for_path(identity.canonical) is not None
    if upload_eligible:
        artifact = registration_events.resolve_upload_path(identity.canonical, db=db)
        if artifact is not None:
            return artifact, True
    if not include_retired:
        return None, False
    if find_workspace(identity, db=db) is not None:
        return None, True
    if upload_eligible:
        return None, registration_events.select_upload_path(identity.canonical, active_only=False, db=db) is not None
    return None, False


def resolve_active(identity: FileIdentity, *, db):
    """Resolve workspace logical, legacy, then eligible upload ownership."""
    return _read(identity, include_retired=False, db=db)[0]


def inspect(identity: FileIdentity, *, db):
    """Return active owner and whether any routed identity exists."""
    return _read(identity, include_retired=True, db=db)
