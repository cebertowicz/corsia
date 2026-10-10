"""Read and update durable artifact identity metadata without a second store."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from registry.registration_events import _now


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of one local file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_metadata(raw: object) -> dict[str, Any]:
    """Return object-valued identity metadata, treating malformed data as empty."""
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge_identity_metadata(db, artifact_id: str, updates: Mapping[str, Any]) -> None:
    """Merge keys into one identity's metadata JSON inside the caller transaction."""
    row = db.execute(
        "SELECT metadata FROM artifact_identities WHERE id=?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise ValueError("artifact identity not found")
    metadata = parse_metadata(row["metadata"])
    metadata.update(updates)
    db.execute(
        "UPDATE artifact_identities SET metadata=?,updated_at=? WHERE id=?",
        (json.dumps(metadata, sort_keys=True), _now(), artifact_id),
    )


def last_written_sha256(raw: object) -> str | None:
    """Read the stored last-sanctioned-write digest from identity metadata."""
    value = parse_metadata(raw).get("last_written_sha256")
    return value if isinstance(value, str) and value else None
