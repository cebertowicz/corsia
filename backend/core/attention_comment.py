"""Measure the words an agent writes into a document's comment threads.

The person reads an agent's comments and replies in a document as they read
its updates, so ``core.attention_notice`` counts a comment's body the same way.
The body is the one the call sent, counted once the result shows the call was
not refused. A comment the writer line saved to post later (``Queued``) counts
when it is saved, so posting it unchanged (``post_as_is``, which takes no body)
does not count it again. Editing a comment's body counts the new body as an
edit. Nothing is read from the database and no model is called.
"""

from __future__ import annotations

from typing import Any

from core.attention_measure import Measure, measure_text

#: What each comment tool hands over: a new comment or reply, or an edit of one's body.
COMMENT_HANDOVERS = {"doc_comment_create": "comment", "doc_comment_update": "comment_edit"}


def measure_comment(tool_name: str, arguments: dict, result: Any) -> tuple[str, Measure | None]:
    """Return the hand-over and the measure of the body the call saved; no measure when none was."""
    handover = COMMENT_HANDOVERS[tool_name]
    body = arguments.get("body")
    # The comment tools report every refusal as text that starts with "Error";
    # a region comment's success can be an image result rather than text.
    refused = result is None or (isinstance(result, str) and result.startswith("Error"))
    if refused or not isinstance(body, str) or not body.strip():
        return handover, None
    return handover, measure_text(body)
