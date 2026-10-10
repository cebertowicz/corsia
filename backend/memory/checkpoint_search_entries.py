"""Adapt canonical frozen continuity entries into compact search results."""

from __future__ import annotations

from collections.abc import Mapping
import json

from core.continuity_render import (
    ContinuitySnapshotEntry,
    ContinuitySnapshotUnreadable,
    read_continuity_snapshot,
)

NARRATIVE_SNIPPET_CHARS = 600
STATEMENT_SNIPPET_CHARS = 600


def generated_at(row: Mapping[str, object]) -> str:
    """Return the installed time, falling back to capture time."""
    return str(row.get("installed_at") or row.get("captured_at") or "")


def snippet(text: str | None, limit: int = NARRATIVE_SNIPPET_CHARS) -> str:
    """Collapse whitespace and bound text shown in a search result."""
    normalized = " ".join((text or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


def continuity_entries(
    snapshot_json: str | None,
) -> tuple[list[ContinuitySnapshotEntry], bool]:
    """Use the shared snapshot decoder, reporting corrupt snapshots."""
    try:
        return read_continuity_snapshot(snapshot_json), False
    except ContinuitySnapshotUnreadable:
        return [], True


def generation_summary(row: Mapping[str, object]) -> dict:
    """Shape a compact generation listing without its carried continuity."""
    return {
        "checkpoint_id": row["id"],
        "thread_id": row["thread_id"],
        "through_seq": row["through_seq"],
        "generated_at": generated_at(row),
        "narrative": snippet(str(row.get("raw_result") or "")),
        "historical": bool(row.get("historical")),
        "history_epoch": row.get("history_epoch"),
    }


def build_entry_hit(
    row: Mapping[str, object],
    entry: ContinuitySnapshotEntry,
    canonical: Mapping[str, object] | None,
    pointers: Mapping[str, object] | None,
) -> dict:
    """Return an entry hit using canonical lifecycle state and bounded pointers."""
    state, current_revision, retired_reason = _entry_state(entry, canonical)
    hit = {
        "kind": "continuity_entry",
        "thread_id": row["thread_id"],
        "statement": snippet(entry.primary_text, STATEMENT_SNIPPET_CHARS),
        "first_checkpoint": _checkpoint_pointer(pointers, "first"),
        "latest_checkpoint": _checkpoint_pointer(pointers, "latest"),
        "entry_state": state,
        "historical": bool(row.get("historical")),
        "history_epoch": row.get("history_epoch"),
        "entry_id": entry.entry_id,
        "field": entry.field,
    }
    if current_revision is not None:
        hit["current_revision"] = current_revision
    if retired_reason is not None:
        hit["retired_reason"] = retired_reason
    return hit


def _checkpoint_pointer(
    pointers: Mapping[str, object] | None, prefix: str
) -> dict | None:
    """Shape an optional first/latest pointer without assuming it still exists."""
    if not pointers:
        return None
    checkpoint_id = pointers.get(f"{prefix}_id")
    if checkpoint_id is None:
        return None
    return {
        "checkpoint_id": checkpoint_id,
        "generated_at": str(pointers.get(f"{prefix}_generated_at") or ""),
    }


def _entry_state(
    entry: ContinuitySnapshotEntry, canonical: Mapping[str, object] | None
) -> tuple[str, int | None, str | None]:
    """Compare one frozen entry with its canonical continuity row."""
    if canonical is None:
        return "missing", None, None
    if canonical["state"] == "retired":
        return "retired", None, str(canonical["retired_reason"])
    revision = canonical["revision"]
    current_revision = revision if isinstance(revision, int) else int(str(revision))
    canonical_body = json.loads(str(canonical["body"]))
    if (
        entry.field == canonical["field"]
        and entry.revision == current_revision
        and entry.body == canonical_body
    ):
        return "current", None, None
    return "revised", current_revision, None


def build_narrative_hit(row: Mapping[str, object], *, unreadable: bool) -> dict:
    """Return a compact result when no continuity entry matched."""
    return {
        "kind": "narrative",
        "checkpoint_id": row["id"],
        "thread_id": row["thread_id"],
        "through_seq": row["through_seq"],
        "generated_at": generated_at(row),
        "narrative": snippet(str(row.get("raw_result") or "")),
        "continuity_unreadable": unreadable,
        "historical": bool(row.get("historical")),
        "history_epoch": row.get("history_epoch"),
    }
