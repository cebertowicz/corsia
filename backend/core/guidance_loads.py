"""Write guidance receipts inside the caller's transaction."""
from __future__ import annotations

from pathlib import Path
import uuid

from core.guidance_manifest import _sha256_bytes, git_blob_oid
from infra.time import utc_now_iso


def insert_guidance_load(
    tx, *, thread_id: str | None, turn_id: str | None, path: Path, data: bytes,
    returned_text: str, partial: bool, node_id: str | None = None,
    variant: str | None = None, checkpoint_generation_id: str | None = None,
    lesson_id: str | None = None, lesson_behavioral_sha256: str | None = None,
    omission_reason: str | None = None,
) -> None:
    tx.execute(
        """INSERT INTO guidance_loads
               (id, thread_id, turn_id, path, raw_sha256, git_blob_oid,
                delivered_sha256, byte_count, partial, node_id, variant,
                checkpoint_generation_id, lesson_id, lesson_behavioral_sha256, omission_reason, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid.uuid4().hex, thread_id, turn_id, str(path), _sha256_bytes(data), git_blob_oid(data),
         _sha256_bytes(returned_text.encode("utf-8")), len(data), int(partial), node_id, variant,
         checkpoint_generation_id, lesson_id, lesson_behavioral_sha256, omission_reason, utc_now_iso()),
    )
