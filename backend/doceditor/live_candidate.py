"""Build one lane operation's candidate text on a fork of the live document."""
from __future__ import annotations

from doceditor.live_document import file_hash
from doceditor.live_text import apply_text_async, clone


async def build_candidate(live, operation):
    """Return the fork, accepted and skipped suggestions, receipt and author.

    The author is the state the operation's author now holds and its text
    hash, taken here from the doc in hand rather than by decoding the state.
    """
    from doceditor.live_lane import EditRefused, LaneRefused
    fork = clone(live.doc.get_update())
    accepted, skipped, receipt = (), (), None
    author = None
    if operation.kind == "accept":
        from doceditor.live_suggestions import suggestion_text
        text, accepted, skipped = suggestion_text(live, operation)
        await apply_text_async(fork, text)
    elif operation.kind == "write":
        base = live.bases.get(operation.base_hash)
        if base is None:
            raise LaneRefused("This document changed since you read it; read it again, then write")
        agent = clone(base)
        before = agent.get_state()
        unchanged = str(agent["text"]) == operation.content
        await apply_text_async(agent, operation.content)
        author = agent.get_update(), file_hash(str(agent["text"]).encode("utf-8"))
        fork.apply_update(agent.get_update(before))
        if unchanged:
            receipt = "No changes needed"
    elif operation.kind == "edit":
        from tools.filesystem_edit import match_edits
        text, receipt = match_edits(str(fork["text"]), operation.edits, str(live.path),
                                   multi=operation.multi_edit, error_path=operation.edit_path)
        if text is None:
            raise EditRefused(receipt)
        await apply_text_async(fork, text)
    elif operation.kind == "replace":
        await apply_text_async(fork, operation.content)
    elif operation.kind != "flush":
        raise LaneRefused("Unknown live document operation")
    if author is None:
        author = fork.get_update(), file_hash(str(fork["text"]).encode("utf-8"))
    return fork, accepted, skipped, receipt, author
