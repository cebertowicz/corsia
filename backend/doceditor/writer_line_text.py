"""The words a writer-line wake and drop notice show an agent: its saved writes and what changed since."""

from __future__ import annotations

import json
from typing import Any

from doceditor import comments as store
from doceditor.note_names import document_name
from registry import models as registry_models

#: Items posted since the save that a wake quotes; the rest are counted.
SINCE_LIMIT = 20


def describe(op: dict[str, Any]) -> str:
    """One saved write in the words a wake shows it."""
    kind = op.get("kind")
    if kind == "reply":
        return f'Reply: "{op["input"]["body"]}"'
    if kind == "decide":
        verb = "Accept" if op["decision"] == "accept" else "Reject"
        reason = f' Reason: "{op["reason"]}"' if op.get("reason") else ""
        return f"{verb} the suggestion.{reason}"
    fields = op.get("fields", {})
    parts = []
    if fields.get("resolved") is True:
        parts.append("Resolve the thread.")
    if fields.get("resolved") is False:
        parts.append("Re-open the thread.")
    if fields.get("acked") is False:
        parts.append("Take back your Agree.")
    if "body" in fields:
        parts.append(f'Edit your comment to: "{fields["body"]}"')
    if any(key in fields for key in ("from_off", "to_off", "anchor_text", "region")):
        parts.append("Move your comment.")
    return " ".join(parts) or "Update the comment."


def wake_text(db, row) -> str:
    root_id = str(row["root_id"])
    root = store.get_comment(root_id, db=db) or {}
    artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
    where = f'the comment thread on "{root.get("anchor_text")}"' if root.get("anchor_text") else "a comment thread"
    since = db.execute(
        """SELECT author, body, created_at FROM doc_comments WHERE (id=? OR parent_id=?) AND created_at>?
           ORDER BY created_at, id""",
        (root_id, root_id, row["saved_at"]),
    ).fetchall()
    lines = [f"The writer turn is yours on {where} in {document_name(artifact)}."]
    if since:
        lines.append("Posted on the thread since you saved your write:")
        lines += [f'- {item["author"]}: "{item["body"]}"' for item in since[:SINCE_LIMIT]]
        if len(since) > SINCE_LIMIT:
            lines.append(f'- and {len(since) - SINCE_LIMIT} more; read them with doc_comments_list(comment_id="{root_id}").')
    else:
        lines.append("Nothing was posted on the thread since you saved your write.")
    if root.get("decision"):
        lines.append(f"The suggestion is now {root['decision']} by {root.get('decided_by') or 'someone'}.")
    elif root.get("resolved"):
        lines.append("The thread is now resolved.")
    lines.append("Your saved write:")
    lines += [f"- {describe(op)}" for op in json.loads(row["ops"])]
    lines += [
        "Answer one way:",
        f'- Post it as is: doc_comment_update(comment_id="{root_id}", post_as_is=true)',
        "- Or rewrite it: make the call again with what you want to say now.",
        "If this turn ends with neither, your write drops out of line and the next agent goes.",
    ]
    return "\n".join(lines)
