"""What a document shows the person once an agent handed it over: the version that passed.

An agent can write a document after its pass by any path, the shell included, so the check
sits where the person reads it. When the file's bytes differ from the passed version:

- a newer version the person wrote themselves has already moved the pass where it was written
  (``core.fresh_read_person_save``: ``person_edit``, no read), so it never differs here;
- otherwise both opens serve the passed bytes, read-only for text (comments, replies and
  suggestion decisions stay open), and one background read of the current bytes starts,
  once per document and version in flight. On a pass the view moves. On a fail the holding
  agent is told once, with the reader's findings, and the passed copy opens editable; the
  person's save keeps the agent's bytes in history (or the holding agent's scratch folder) and
  wakes that agent once. When no reader could run, nothing is decided: the passed version
  stays shown read-only and the next open reads the newer version again.

A document no agent has handed over has no view and is served as it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from core import fresh_read
from core.fresh_read import Document, Item, Verdict
from core.fresh_read_store import note_recheck, sha256_bytes, view
from infra.db_async import run_in_db

NOTICE_WAITING = ("An agent's newer version of this document hasn't passed its automatic read yet. "
                  "You're seeing the last version that passed.")
NOTICE_FAILED = ("An agent's newer version of this document didn't pass its automatic read. "
                 "You're seeing the last version that passed.")
_running: set[tuple[str, str]] = set()


def document_pass(artifact_id: str, data: bytes) -> str | None:
    """Blocking. The pass already covering exactly these bytes of the document, if any."""
    row = view(artifact_id)
    return str(row["fresh_read_id"]) if row is not None and row["sha256"] == sha256_bytes(data) else None


async def read_document(artifact_id: str, name: str, data: bytes, *, kind: str, writer_thread_id: str | None,
                        user_id: str | None, node_id: str | None = None, note: str | None = None) -> Verdict:
    """Pass a document hand-over, reusing a pass that already covers these exact bytes."""
    existing = await run_in_db(document_pass, artifact_id, data)
    if existing is not None:
        return Verdict(True, existing)
    text = data.decode("utf-8", errors="replace")
    return await fresh_read.check(Item(kind=kind, item_ref=artifact_id, shown=text, writer_thread_id=writer_thread_id,
                            user_id=user_id, node_id=node_id, note_to_reader=note,
                            documents=(Document(artifact_id, name, data),)))


async def _recheck(artifact_id: str, path: Path, data: bytes, holder: str | None, node_id: str | None) -> None:
    sha = sha256_bytes(data)
    try:
        verdict = await read_document(artifact_id, path.name, data, kind="document_recheck",
                                      writer_thread_id=holder, user_id=None, node_id=node_id)
        if not verdict.passed:
            await run_in_db(note_recheck, artifact_id, sha, "held" if verdict.held else "failed")
        if not verdict.passed and not verdict.held and holder:
            await _tell_holder(holder, artifact_id, path, sha, verdict.findings)
    except Exception:  # noqa: BLE001  # a failed recheck leaves the passed version shown; the next open retries
        logger.exception("fresh read: background read of {} failed", artifact_id)
    finally:
        _running.discard((artifact_id, sha))


async def _tell_holder(holder: str, artifact_id: str, path: Path, sha: str,
                       findings: tuple[dict[str, Any], ...]) -> None:
    """Tell the holding agent, once per version, that its newer version is not what the person sees."""
    from events.delivery import deliver_to_thread

    # The findings alone: the door's "call again" advice does not apply to a background read.
    lines = fresh_read.finding_lines(findings)
    summary = (f"Your newer version of {path.name} did not pass its fresh read, so the person still sees the "
               f"version that passed. Revise the file; its next open reads it again.\n{lines}").rstrip()
    try:
        await deliver_to_thread(holder, "fresh_read.recheck_failed", summary,
                                {"artifact_id": artifact_id, "path": str(path), "sha256": sha},
                                delivery_id=f"fresh-read-recheck-failed:{artifact_id}:{sha}")
    except Exception:  # noqa: BLE001  # the person still sees the passed version; a missed wake is logged
        logger.exception("fresh read: telling {} its recheck failed did not reach it", holder)


def start_recheck(artifact_id: str, path: Path, data: bytes, holder: str | None, node_id: str | None) -> None:
    """One background read per document and version, started from an open or an accepted suggestion."""
    from infra import app_loop

    key = (artifact_id, sha256_bytes(data))
    if key in _running:
        return
    _running.add(key)
    try:
        app_loop.submit(_recheck(artifact_id, path, data, holder, node_id))
    except app_loop.AppLoopUnavailable:
        _running.discard(key)


def served(artifact: dict[str, Any] | None, path: Path, raw: bytes, writable: bool) -> dict[str, Any] | None:
    """Blocking. What an open serves instead of the file, or None to serve the file as it is."""
    if not artifact:
        return None
    row = view(str(artifact["id"]))
    current = sha256_bytes(raw)
    if row is None or row["sha256"] == current or row["body"] is None:
        return None
    # A hold (no reader could run) decides nothing: the next open reads this version again.
    failed = row["recheck_sha256"] == current and row["recheck_verdict"] == "failed"
    if not failed:
        start_recheck(str(artifact["id"]), path, raw, row["holder_thread_id"], row["node_id"])
    return {
        "text": bytes(row["body"]).decode("utf-8", errors="replace"),
        "writable": writable and failed,
        "passed_version": {"sha256": row["sha256"], "fresh_read_id": row["fresh_read_id"],
                           "current_sha256": current, "notice": NOTICE_FAILED if failed else NOTICE_WAITING},
    }
