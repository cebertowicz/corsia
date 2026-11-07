"""The wake an agent gets when a document section's writer turn is its turn (``writer_line.serve``).

It says what changed in the section since the agent saved its write, counts what changed in every
other section since the agent last read the document (``section_log`` for edits, comment rows for
comments and suggestions), and shows the saved write with the two ways to answer: make the same call
again to post it as is, or make the call it wants now.
"""

from __future__ import annotations

import bisect
import difflib
import json
from typing import Any

from doceditor import doc_sections, writer_turns
from doceditor.doc_text import read_doc_text
from doceditor.note_names import document_name
from doceditor.quote_anchor import _cp_at_u16
from registry import models as registry_models

#: Diff lines a wake quotes; the rest are counted.
DIFF_LIMIT = 60
#: Most comment rows a wake reads to count them.
COMMENT_LIMIT = 1000
#: Most characters of one saved string a wake quotes.
QUOTE_LIMIT = 400


def _clip(value: str | None) -> str:
    value = value or ""
    return value if len(value) <= QUOTE_LIMIT else f"{value[:QUOTE_LIMIT]}... ({len(value)} characters)"


def _current(artifact: dict[str, Any] | None) -> str | None:
    try:
        return read_doc_text(artifact.get("path")) if artifact else None
    except (OSError, ValueError):
        return None


def _diff(before: str, after: str | None) -> list[str]:
    if after is None:
        return ["- The section is no longer in the document (deleted or renamed); read the document."]
    if before == after:
        return ["Nothing changed in it since you saved your write."]
    lines = [line.rstrip("\n") for line in difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), "as you saved it", "now", n=1)]
    shown = lines[:DIFF_LIMIT]
    if len(lines) > DIFF_LIMIT:
        shown.append(f"... and {len(lines) - DIFF_LIMIT} more diff lines; read the document.")
    return ["What changed in it since you saved your write:", "```diff", *shown, "```"]


def _comment_counts(db, artifact_id: str, thread_id: str, since: str, text: str) -> dict[str, list[int]]:
    """New top-level comments and suggestions by others since ``since``, per section: [comments, suggestions]."""
    rows = db.execute(
        """SELECT from_off, replacement FROM doc_comments
           WHERE artifact_id=? AND created_at>? AND parent_id IS NULL AND author_thread_id IS NOT ?
           ORDER BY created_at LIMIT ?""",
        (artifact_id, since, thread_id, COMMENT_LIMIT),
    ).fetchall()
    marks = doc_sections.headings(text)
    starts = [start for start, _ in marks]
    counts: dict[str, list[int]] = {}
    for row in rows:
        offset = _cp_at_u16(text, row["from_off"]) if row["from_off"] is not None else None
        heading = marks[bisect.bisect_right(starts, offset) - 1][1] if offset is not None else ""
        counts.setdefault(heading, [0, 0])[row["replacement"] is not None] += 1
    return counts


def _elsewhere(db, row, heading: str, text: str | None) -> list[str]:
    from doceditor.live_tools import find_live_artifact

    entry = find_live_artifact(str(row["artifact_id"]))
    thread_id = str(row["agent_thread_id"])
    edits = entry.live.sections.since(thread_id) if entry is not None else None
    if edits is None or text is None:
        return ["Changes in the document's other sections since you last read it are unknown "
                "(it was closed or the server restarted): read the document before you answer."]
    comments = _comment_counts(db, str(row["artifact_id"]), thread_id, entry.live.sections.read_at[thread_id], text)
    parts: list[str] = []
    for name in dict.fromkeys([*edits, *comments]):
        if name == heading:
            continue
        noted, suggested = comments.get(name, [0, 0])
        counted = [f"{number} {label}{'' if number == 1 else 's'}" for number, label in (
            (edits.get(name, 0), "edit"), (noted, "comment"), (suggested, "suggestion")) if number]
        parts.append(f'- {doc_sections.name(name)}: {", ".join(counted)}')
    if not parts:
        return ["Nothing changed in the document's other sections since you last read it."]
    return ["Changed in other sections since you last read the document:", *parts]


def describe(op: dict[str, Any]) -> str:
    """One saved section write in the words a wake shows it."""
    call = op["call"]
    if op["kind"] == "suggest":
        return (f'Suggest replacing "{_clip(call["quote"])}" with "{_clip(call["replacement"])}" (doc_comment_create). '
                f'Comment: "{_clip(call["body"])}"')
    if op["kind"] == "write":
        return f'Rewrite the whole document (write_file, {len(call["content"])} characters).'
    edits = "; ".join(f'"{_clip(edit["old_string"])}" -> "{_clip(edit["new_string"])}"' for edit in call["edits"])
    return f'{call["tool"]} on {call["path"]}: {edits}'


def wake_text(db, row) -> str:
    heading = writer_turns.section_heading(str(row["root_id"])) or ""
    artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
    text = _current(artifact)
    ops = json.loads(row["ops"])
    lines = [f"The writer turn is yours on {doc_sections.name(heading)} in {document_name(artifact)}."]
    current = doc_sections.section_text(text, heading) if text is not None else None
    lines += _diff(next((op["base"][heading] for op in ops if heading in op.get("base", {})), ""), current)
    lines += _elsewhere(db, row, heading, text)
    lines.append("Your saved write:")
    lines += [f"- {describe(op)}" for op in ops]
    lines += [
        "Answer one way:",
        "- Post it as is: make the same call again, with the same arguments.",
        "- Or rewrite it: make the call you want now.",
        "If this turn ends with neither, your write drops out of line and the next agent goes.",
    ]
    return "\n".join(lines)
