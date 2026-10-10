"""Schema 103 -> 104: type workspace image files ``image``.

Before image typing, a workspace file such as ``shot.png`` registered as
``code``. New registrations of the same kind of file are ``image`` and an
identity is looked up by its type, so a row left as ``code`` would no longer be
found for its own file. This retypes those rows, through the same extension
table the live classifier reads, and changes nothing but ``type``. Rows already
typed ``image`` are left alone, so a second run changes nothing; a ``code`` row
whose file already has an ``image`` identity is left as it is, because the
``(integration, type, external_id)`` key allows only one. Runs inside the
runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from registry.file_types import image_media_type


def migrate_103_to_104(conn: sqlite3.Connection) -> None:
    """Retype each legacy image row; a database with no identity table has nothing to retype."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_identities'").fetchone() is None:
        return
    rows = conn.execute(
        "SELECT id, integration, external_id, path FROM artifact_identities "
        "WHERE integration = 'workspace' AND type = 'code'"
    ).fetchall()
    for identity_id, integration, external_id, path in rows:
        if image_media_type(Path(path or external_id)) is None:
            continue
        conn.execute(
            "UPDATE artifact_identities SET type = 'image' "
            "WHERE id = ? AND NOT EXISTS ("
            "  SELECT 1 FROM artifact_identities twin"
            "   WHERE twin.integration = ? AND twin.type = 'image' AND twin.external_id = ?)",
            (identity_id, integration, external_id),
        )
