"""Schema 112 -> 113: type local image uploads ``image``.

Before upload image typing, a local upload such as ``photo.png`` registered as
``upload``. New registrations of the same kind of file are ``image`` and an
identity is looked up by its type, so a row left as ``upload`` would no longer
be found for its own file. This retypes those rows, through the same extension
table the live classifier reads, and changes nothing but ``type``. Rows already
typed ``image`` are left alone, so a second run changes nothing; an ``upload``
row whose file already has an ``image`` identity is left as it is, because the
``(integration, type, external_id)`` key allows only one. Runs inside the
runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from registry.file_types import image_media_type


_UPLOAD_PATH_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_artifact_identities_upload_path "
    "ON artifact_identities(path) WHERE integration='upload'"
)


def migrate_112_to_113(conn: sqlite3.Connection) -> None:
    """Retype local upload images and index active upload-path resolution."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_identities'").fetchone() is None:
        return
    rows = conn.execute(
        "SELECT id, integration, external_id, path FROM artifact_identities "
        "WHERE integration = 'upload' AND type = 'upload'"
    ).fetchall()
    for identity_id, integration, external_id, path in rows:
        if (
            not isinstance(path, str)
            or urlparse(path).scheme
            or not Path(path).is_absolute()
            or image_media_type(Path(path)) is None
        ):
            continue
        conn.execute(
            "UPDATE artifact_identities SET type = 'image' "
            "WHERE id = ? AND NOT EXISTS ("
            "  SELECT 1 FROM artifact_identities twin"
            "   WHERE twin.integration = ? AND twin.type = 'image' AND twin.external_id = ?)",
            (identity_id, integration, external_id),
        )
    conn.execute(_UPLOAD_PATH_INDEX_DDL)
