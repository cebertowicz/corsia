"""The documents an update hands the person: exactly those chat shows as openable cards under it.

One resolver for every reader of that set: the attention notice measures them, the fresh read
reads them. It takes the text, roots and second origin that ``api.thread_cards.attach_cards``
gives the cards, so links, bare paths, ``file.md:12`` citations, decoding, fenced-code
exclusion and the scope check are the cards' own (``registry.path_cards``);
``test_attention_notice_update_links`` holds the two together.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: The card type of a document the person reads.
READ_TYPE = "document"


def linked_documents(db, text: str, user_id: str, project_id: str | None) -> list[tuple[str | None, Path]]:
    """Each readable document card ``text`` gives the person, as ``(artifact id or None, path)``."""
    from doceditor.document_scope import scope_roots
    from infra import config
    from registry.path_cards import reference_cards

    origin, bases = scope_roots(project_id, db)
    cards = reference_cards(
        db, text, bases, str(user_id), origin=origin,
        fallback_origin=config.WORKSPACE.resolve() if project_id else None,
    )
    found: list[tuple[str | None, Path]] = []
    for card in cards:
        if card.get("type") != READ_TYPE or not card.get("available"):
            continue
        artifact_id = str(card["artifact_id"]) if card.get("artifact_id") else None
        path = artifact_path(db, artifact_id) if artifact_id else Path(str(card["path"]))
        if path is not None and all(path != seen for _id, seen in found):
            found.append((artifact_id, path))
    return found


def artifact_path(db, artifact_id: str) -> Path | None:
    """Where a registered document's file is now, or None when it has none."""
    from infra import config
    from infra.workspace_paths import resolve_registered_path
    from registry import models as registry_models

    artifact = registry_models.get_artifact(artifact_id, db=db)
    if artifact is None:
        return None
    try:
        raw = resolve_registered_path(artifact.get("external_id"), artifact.get("path"))
    except ValueError:
        log.warning("update documents: document %s is outside this workspace", artifact_id)
        return None
    raw = raw or artifact.get("external_id")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else config.WORKSPACE / path
