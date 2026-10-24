"""Live suggestion adapter; canonical quote and batch rules stay in comment_actions."""
from __future__ import annotations

import json
from pathlib import Path

from doceditor.document_scope import UI_THREAD, revision
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_tools import find_live


def suggestion_text(live, operation: Operation):
    """Resolve immutable input at the lane start, refusing changed tracked quotes."""
    from doceditor.comment_actions import CommentActionError, STALE_MESSAGE
    from doceditor.comment_batch_actions import _batch_text
    rows = [json.loads(value) for value in operation.suggestions]
    valid, skipped = [], []
    for row in rows:
        if row["id"] in live.ranges:
            if live.range_text(row["id"]) != row["anchor_text"]:
                if operation.single_suggestion:
                    raise CommentActionError("stale", STALE_MESSAGE)
                skipped.append({"id": row["id"], "reason": STALE_MESSAGE})
                continue
            row["from_off"], row["to_off"] = live.ranges[row["id"]]
        valid.append(row)
    text, accepted, batch_skipped = _batch_text(valid, live.text)
    if operation.single_suggestion and not accepted:
        raise CommentActionError("stale", STALE_MESSAGE)
    return text, tuple(accepted), tuple(skipped + batch_skipped)


async def accept_live(artifact, project_id, rows, actor_thread_id, *, user_id=None, single=False,
                      suggestion_accept=False):
    """Return None only for a legacy file; all adapters share this live branch."""
    from doceditor.comment_actions import CommentActionError, suggestion_span
    entry = find_live(Path(artifact["path"]))
    if entry is None:
        return None
    live = entry.live
    # Register a range before queueing so browser changes during the wait map it.
    for row in rows:
        if row["id"] not in live.ranges:
            try:
                start, end = suggestion_span(row, live.text)
            except CommentActionError:
                continue
            live.track_range(row["id"], len(live.text[:start].encode("utf-16-le")) // 2,
                             len(live.text[:end].encode("utf-16-le")) // 2)
    actor = WriteActor(actor_thread_id or UI_THREAD, project_id, direct_user=actor_thread_id is None, user_id=user_id,
                       suggestion_accept=suggestion_accept)
    from doceditor.document_save import DocumentSaveError
    from doceditor.live_lane import LaneRefused
    from doceditor.live_store import StoreRefused
    from registry.shared_write_errors import DurableWritePolicyError
    try:
        result = await entry.lane.submit(Operation("accept", actor=actor,
            suggestions=tuple(json.dumps(row) for row in rows), single_suggestion=single))
        result.require_landed()
    except (LaneRefused, StoreRefused) as exc:
        raise CommentActionError("save_refused", str(exc)) from exc
    except DurableWritePolicyError as exc:
        raise DocumentSaveError("durable_write_policy", str(exc)) from exc
    return {"accepted": list(result.accepted), "skipped": list(result.skipped), "text": result.text,
            "revision": revision(result.text.encode("utf-8")), "server_saved": result.server_saved}
