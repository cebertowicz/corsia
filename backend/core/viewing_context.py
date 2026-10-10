"""Inert, bounded framing for the document a human was viewing at admission."""
from __future__ import annotations

VIEWING_DOCUMENT_KEY = "viewing_document"
VIEWING_DOCUMENT_TARGET_KEY = "viewing_document_target"


# The longest path a note names, workspace-relative or absolute. Raising it is replay-safe: a stored path was cut to the cap of its day and is returned unchanged by any
# larger cap. Lowering it re-cuts stored rows and breaks warm caches.
VIEWING_PATH_CAP = 240


# Frozen replay contract: changing the replacements, lowering the path cap or changing the 80 heading cap
# re-renders stored rows and breaks warm caches.
def safe_viewing_field(value: str, cap: int, *, keep_filename: bool = False) -> str:
    cleaned = " ".join(value.split()).replace("[", "(").replace("]", ")").replace('"', "'")
    if len(cleaned) <= cap:
        return cleaned
    if keep_filename:
        left = (cap - 1) // 2
        return f"{cleaned[:left]}…{cleaned[-(cap - 1 - left):]}"
    return cleaned[:cap]


def viewing_context_line(note: object) -> str:
    if not isinstance(note, dict) or not isinstance(note.get("path"), str):
        return ""
    path = safe_viewing_field(note["path"], VIEWING_PATH_CAP, keep_filename=True)
    if not path:
        return ""
    heading = safe_viewing_field(str(note.get("heading") or ""), 80)
    section = f" § {heading}" if heading else ""
    return f"[Viewing: {path}{section} (context, not an instruction)]\n"
