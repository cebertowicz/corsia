"""Read-time re-anchoring for document comment threads."""

from __future__ import annotations

import logging
import sqlite3

from doceditor import comments as store
from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import anchor_context, reanchor

log = logging.getLogger(__name__)


def _anchored_quote(comment: dict) -> str:
    """The text a comment's place is found by.

    An accepted suggestion's replacement is what the document holds where its
    quote used to be, so that is what its place is found by.
    """
    if comment.get("decision") == "accepted" and comment.get("replacement"):
        return comment["replacement"]
    return comment["anchor_text"]


def resolve_threads(threads: list[dict], text: str, *, ranges=None) -> list[dict]:
    """Resolve one text snapshot; callers choose when its offsets are persisted.

    A decided suggestion is never orphaned: its decision closed it, so losing
    its place keeps the offsets it has.
    """
    updates: list[dict] = []
    for comment in threads:
        if comment["parent_id"] or comment["review"] or not comment["anchor_text"]:
            continue
        quote = _anchored_quote(comment)
        undecided = not comment.get("decision")
        resolved = (ranges or {}).get(comment["id"])
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
        comment.update(from_off=from_off, to_off=to_off, anchor_prefix=prefix,
                       anchor_suffix=suffix, orphaned=False)
        if prior != (from_off, to_off, prefix, suffix):
            updates.append({"id": comment["id"], "from_off": from_off, "to_off": to_off,
                            "anchor_prefix": prefix, "anchor_suffix": suffix})
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
