"""The type and title an artifact identity carries after its file moves."""

from __future__ import annotations

from pathlib import Path

from registry import service


def target_identity(
    identity: dict, source_p: Path, target_p: Path,
) -> tuple[str, str | None]:
    """Return the ``(type, title)`` the identity must carry after the move.

    Type is always re-derived: it is a pure function of the path, and a stale
    one forks identity. Title is only re-derived when it is still the
    file-derived default ``path.name`` that registration assigns — an
    explicitly chosen title survives the move.

    ``service.classify`` only knows the workspace document/code/skill types,
    so this is meaningful only for ``workspace`` identities; callers reject
    every other integration before reaching here.
    """
    artifact_type = service.classify(target_p)
    title = identity.get("title")
    if title is None or title == source_p.name:
        title = target_p.name
    return artifact_type, title
