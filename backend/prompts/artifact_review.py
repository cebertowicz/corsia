"""Instructions for disposable document reviews and their ordinary continuations."""

from __future__ import annotations


DEFAULT_INTENT = "Review the document for accuracy, scope and actionable issues."
MAX_CRITERIA_BYTES = 1000

_STOP_REASONS = {
    "missing_credentials": "sign-in is needed",
    "target_unavailable": "the model was unavailable",
    "limit": "it reached its review limit",
    "time_limit": "it ran out of time",
    "interrupted": "it was interrupted",
}

# Reasons a review was handed back before it started, with the continuation the reviewer reads.
_START_REASONS = {
    "document_has_images": (
        "Review update: A separate review could not start because the document has images. "
        "Review the document here instead: open each image with read_file, then comment and call "
        "doc_feedback_send. If an image cannot be opened, say which one in a comment."
    ),
    "document_unreadable": (
        "Review update: A separate review could not start because the document could not be read as text. "
        "Review the document here instead, then call doc_feedback_send."
    ),
}


def requesting_task_block(task: dict) -> str:
    """The requesting task as the fork reads it, opening with the revision that was read.

    The task is the canonical intent holder, so a delegated worker's request names its parent's task.
    """
    lines = [
        f'Requested from task "{task["title"]}" ({task["node_id"]}), '
        f"requirements revision {task['requirements_revision']}."
    ]
    if task["intent"]:
        lines.append(f"Intent: {task['intent']}")
    # Intent and quotes are capped where Plans writes them; acceptance criteria are not, so cap them here.
    encoded = task["acceptance_criteria"].encode()
    criteria = encoded[:MAX_CRITERIA_BYTES].decode(errors="ignore")
    cut = " [truncated]" if len(encoded) > MAX_CRITERIA_BYTES else ""
    lines.append(f"Acceptance criteria: {criteria}{cut}")
    lines += [f'{word["author"]} ({word["role"]}): "{word["quote"]}"' for word in task["intent_words"]]
    return "\n".join(lines)


def instruction(row, artifact, task_block: str | None = None) -> str:
    document = artifact["path"] or artifact["external_id"] or row["artifact_id"]
    task = f"\n\n{task_block}" if task_block else ""
    return (
        f"Review the document {document!s}. Request: {row['note'] or DEFAULT_INTENT}{task}\n\n"
        "Use only the document comment tools. Create review comments or suggestions as needed, "
        "then call doc_feedback_send for this document. Do not send messages, spawn work, or write files."
    )


def continuation(comment_count: int, reason: str) -> str:
    """Describe a review hand-back without lifecycle internals."""
    if reason in _START_REASONS:
        return _START_REASONS[reason]
    why = _STOP_REASONS.get(reason, "it could not finish")
    return f"Review update: The review stopped before finishing ({why}). Comments so far: {comment_count}."
