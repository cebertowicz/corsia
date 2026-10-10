"""Known lesson tuples retained on visible installed-context boundaries.

A behavioral association is not an exact per-call install, raw version or cost.
Private prompt material is never included in this report projection.
"""
from __future__ import annotations

from typing import Any

from core.guidance_manifest import SKILLS_INDEX_SOURCE_ID
from core.installed_context import INSTALLED_SOURCES, snapshot_from_metadata
from infra.learning_schema import installed_boundary_guard

# The guard is SQL text, not a bound parameter, so SQLite can match it to the
# partial index's predicate; `installed_boundary_guard` states both from one
# definition. The structural check stays: the key text also appears inside
# ordinary tool content, and only an array is an installed snapshot.
_BOUNDARY_SQL = (
    "SELECT m.id,m.metadata FROM threads t JOIN messages m ON m.thread_id=t.id "
    "WHERE t.id=? AND t.user_id=? "
    f"AND {installed_boundary_guard('m.metadata')} "
    "AND CASE WHEN json_valid(m.metadata) THEN json_type(m.metadata,?) END='array' "
    "ORDER BY m.seq,m.id"
)


def read_boundaries(db, *, user_id: str, thread_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep exact retained tuples; a superseded boundary may have lost its sources."""
    facts: list[dict[str, Any]] = []
    for thread in dict.fromkeys(thread_ids):
        rows = db.execute(
            _BOUNDARY_SQL,
            (thread, user_id, f"$.{INSTALLED_SOURCES}"),
        ).fetchall()
        for row in rows:
            snapshot = snapshot_from_metadata(row["metadata"])
            for source in (snapshot or {}).get("sources", []):
                if not isinstance(source, dict) or source.get("source_id") != SKILLS_INDEX_SOURCE_ID:
                    continue
                if source.get("inclusion") not in {"included", "summarized", "proof"}:
                    continue
                metadata = source.get("metadata")
                lessons = metadata.get("lessons") if isinstance(metadata, dict) else None
                if not isinstance(lessons, list):
                    continue
                for lesson in lessons:
                    if not isinstance(lesson, dict):
                        continue
                    subject, version = lesson.get("id"), lesson.get("behavioral_sha256")
                    if not isinstance(subject, str) or not subject or not isinstance(version, str) or not version:
                        continue
                    facts.append({
                        "kind": "lesson", "id": subject, "version": version, "offered": True,
                        "evidence_kind": "installed boundary", "thread_id": thread,
                        "message_id": row["id"], "name": lesson.get("name"),
                        "path": lesson.get("path"), "raw_sha256": lesson.get("raw_sha256"),
                        "owner_id": lesson.get("scope_id") if lesson.get("scope") == "thread" else None,
                        "inherited": lesson.get("inherited"), "index_line": lesson.get("index_line"),
                        "per_call_install": None, "descriptor_tokens": None,
                        "linkage_reason": "behavioral tuple alone does not bind an offer to an exact install",
                    })
    return facts


def identity_for_raw(facts: list[dict[str, Any]], *, digest: str, path: str) -> dict[str, Any] | None:
    """Same-read raw hash/path tuples can identify behavior, never arbitrary cost."""
    matches = [fact for fact in facts if fact.get("raw_sha256") == digest and fact.get("path") == path]
    identities = {(fact.get("kind"), fact.get("id"), fact.get("version")) for fact in matches}
    if len(identities) != 1:
        return None
    kind, subject, version = next(iter(identities))
    return {"kind": kind, "id": subject, "version": version}
