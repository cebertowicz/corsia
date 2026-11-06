"""Read-time re-anchoring for document comment threads."""

from __future__ import annotations

import logging
import sqlite3

from doceditor import comments as store
from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import _cp_at_u16, anchor_context, reanchor

log = logging.getLogger(__name__)


def _anchored_quote(comment: dict) -> str:
    """The text a comment's place is found by.

    An accepted suggestion's replacement is what the document holds where its
    quote used to be, so that is what its place is found by.
    """
    if comment.get("decision") == "accepted" and comment.get("replacement"):
        return comment["replacement"]
    return comment["anchor_text"]


def _text_in_range(text: str, from16: int, to16: int) -> str:
    """The text between two UTF-16 offsets, or "" for a collapsed or invalid range."""
    start, end = _cp_at_u16(text, from16), _cp_at_u16(text, to16)
    return text[start:end] if start is not None and end is not None and end > start else ""


def resolve_threads(threads: list[dict], text: str, *, ranges=None) -> list[dict]:
    """Resolve one text snapshot; callers choose when its offsets are persisted.

    A decided suggestion is never orphaned: its decision closed it, so losing
    its place keeps the offsets it has.

    A comment whose live range still holds text keeps its place when the text
    inside the range changes: the anchor is the range's two ends, and the text
    between them becomes its new quote. Only a collapsed range, with all of its
    text gone, orphans. A suggestion's quote is the text it replaces, so a
    suggestion keeps the quote rule.
    """
    updates: list[dict] = []
    for comment in threads:
        if comment["parent_id"] or comment["review"] or not comment["anchor_text"]:
            continue
        quote = _anchored_quote(comment)
        undecided = not comment.get("decision")
        resolved = (ranges or {}).get(comment["id"])
        tracked = resolved is not None
        if tracked and undecided and not comment.get("replacement"):
            quote = _text_in_range(text, *resolved) or quote
        if resolved is None:
            resolved = reanchor(
                text, quote, from16=comment["from_off"], to16=comment["to_off"],
                prefix=comment.get("anchor_prefix"), suffix=comment.get("anchor_suffix"),
            )
        if resolved is None:
            comment["orphaned"] = undecided
            continue
        from_off, to_off = resolved
        context = anchor_context(text, from_off, to_off, quote)
        if context is None:
            comment["orphaned"] = undecided
            # Preserve mapped collapsed ranges in the live response. Reload can
            # deliberately omit the range hint and resolve restored text anew.
            if undecided and comment["id"] in (ranges or {}):
                comment["from_off"], comment["to_off"] = resolved
            continue
        prefix, suffix = context
        prior = (comment.get("from_off"), comment.get("to_off"),
                 comment.get("anchor_prefix"), comment.get("anchor_suffix"))
        requoted = undecided and quote != comment["anchor_text"]
        comment.update(from_off=from_off, to_off=to_off, anchor_prefix=prefix,
                       anchor_suffix=suffix, orphaned=False)
        if requoted:
            comment["anchor_text"] = quote
        if prior != (from_off, to_off, prefix, suffix) or requoted:
            update = {"id": comment["id"], "from_off": from_off, "to_off": to_off,
                      "anchor_prefix": prefix, "anchor_suffix": suffix}
            if requoted:
                update["anchor_text"] = quote
            updates.append(update)
    return updates


def prepare_positions(artifact_id: str, text: str, db: sqlite3.Connection, *, ranges=None) -> list[dict]:
    """Prepare file-coordinate offsets before the publisher's DB-only transaction."""
    return resolve_threads(store.list_for_artifact(artifact_id, db=db), text, ranges=ranges)


def anchored_threads(artifact_id: str, path: str | None, db: sqlite3.Connection,
                     *, text: str | None = None, persist: bool = True) -> tuple[list[dict], bool]:
    """Return threads anchored to current text, without making a read fragile.

    A failed text read leaves persisted offsets alone and reports that anchoring
    was skipped. A failed offset/context write also leaves the computed response
    valid; the next read retries persistence.
    """
    threads = store.list_for_artifact(artifact_id, db=db)
    ranges = None
    if text is None and path:
        from pathlib import Path
        from doceditor.live_tools import find_live
        entry = find_live(Path(path))
        if entry is not None:
            text, ranges = entry.live.snapshot
            persist = False
    try:
        if text is None:
            text = read_doc_text(path)
    except (OSError, ValueError) as exc:
        log.info("document comment anchoring skipped for %s: %s", artifact_id, exc)
        return threads, False
    updates = resolve_threads(threads, text, ranges=ranges)
    if updates and persist:
        try:
            store.update_positions(artifact_id, updates, db=db)
        except sqlite3.Error as exc:  # a list response must not fail after re-anchoring
            log.warning("document comment anchor persistence failed for %s: %s", artifact_id, exc)
    return threads, True
