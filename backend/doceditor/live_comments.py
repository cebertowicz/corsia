"""Build comment position frames on the live document's owning event loop."""
from __future__ import annotations

from doceditor import comments
from doceditor.comment_anchoring import resolve_threads
from infra.db_async import run_in_db
from doceditor.live_text import clone, map_offset


def candidate_ranges(live, fork) -> dict[str, tuple[int, int]]:
    """Project the current ranges into exactly the candidate that will reach disk."""
    ranges = dict(live.ranges)
    if not ranges:
        return ranges
    projection = clone(live.doc.get_update())
    text = projection["text"]
    before = str(text)
    def changed(event):
        for identifier, (start, end) in tuple(ranges.items()):
            first = map_offset(start, before, event.delta, right=start == end)
            last = map_offset(end, before, event.delta, right=True)
            ranges[identifier] = (first, max(first, last))
    observer = text.observe(changed)
    try:
        projection.apply_update(fork.get_update())
    finally:
        text.unobserve(observer)
    return ranges


async def positions(entry, artifact_id: str, *, reload: bool = False) -> dict:
    """Resolve and capture ranges, digest and vector together without yielding."""
    rows = await run_in_db(comments.list_for_artifact, artifact_id)
    live = entry.live
    resolve_threads(rows, live.text, ranges=None if reload else live.ranges)
    placed = [row for row in rows
              if not row.get("orphaned") and row.get("anchor_text") and not row["parent_id"] and not row["review"]]
    for row in placed:
        live.track_range(row["id"], row["from_off"], row["to_off"])
    # An orphaned row keeps its collapsed live range for a later resolution; naming it would report a place.
    return {"ranges": [{"id": row["id"], "from_off": live.ranges[row["id"]][0],
                        "to_off": live.ranges[row["id"]][1]} for row in placed],
            "digest": live.digest, "vector": live.doc.get_state()}
