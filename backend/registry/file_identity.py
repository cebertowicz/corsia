"""Filesystem facts prepared before entering an artifact write transaction."""

from dataclasses import dataclass
from typing import Any

from infra.workspace_paths import WorkspaceReference, workspace_selection_for_root


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Resolved path identity, not a snapshot of database ownership or state."""

    canonical: str
    zone: str
    ephemeral_default: bool
    artifact_type: str
    external_id: str


def external_identity_for_path(
    canonical: str,
    zone: str,
    *,
    db=None,
) -> str:
    """Return the stable identity for one normalized physical path."""
    if zone != "workspace":
        return canonical
    from infra import config

    selection = workspace_selection_for_root(config.WORKSPACE.resolve())
    return WorkspaceReference.from_path(canonical, selection).serialize()


@dataclass(frozen=True, slots=True)
class MovedFileRecovery:
    """One missing identity a moved file may adopt, validated on disk beforehand."""

    identity: dict[str, Any]
    move: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class PreparedFileWrite:
    """Every filesystem fact one registration needs, read outside its transaction."""

    identity: FileIdentity
    local_sha256: str | None
    moved_from: MovedFileRecovery | None

    def metadata(self, metadata: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Seed the prepared hash only for an identity no writer has hashed.

        Writers pass ``last_written_sha256`` explicitly; registration never
        replaces a stored one, so a read cannot overwrite a writer's hash.
        """
        if self.local_sha256 is not None and current.get("last_written_sha256") is None:
            metadata.setdefault("last_written_sha256", self.local_sha256)
        return metadata
