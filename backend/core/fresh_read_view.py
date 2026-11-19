"""What a document shows the person once an agent handed it over: the version that passed.

An agent can write a document after its pass by any path, the shell included, so the check
sits where the person reads it. When the file's bytes differ from the passed version:

- a newer version the person saved themselves moves the pass (``person_edit``, no read);
- otherwise both opens serve the passed bytes, read-only for text (comments, replies and
  suggestion decisions stay open), and one background read of the current bytes starts,
  once per document and version. On a pass the view moves; on a fail or no reader the
  passed copy opens editable, and the person's save keeps the agent's bytes in history (or
  the holding agent's scratch folder) and wakes that agent once.

A document no agent has handed over has no view and is served as it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from core import fresh_read
from core.fresh_read import Document, Item, Verdict
from core.fresh_read_store import PERSON_EDIT, ReadRecord, note_recheck, record, sha256_bytes, view
from infra.db_async import run_in_db

NOTICE_WAITING = "A newer version is waiting on its check; this is the last checked version."
NOTICE_FAILED = "A newer version did not pass its check; this is the last checked version."
_running: set[tuple[str, str]] = set()


def _person_wrote(path: Path, data: bytes) -> bool:
    """Whether the newest recorded version of ``path`` holds ``data`` and is a person's own edit."""
    from infra import config
    from infra.document_history import repository
    from infra.guidance_git import _git_bytes
    from infra.guidance_messages import accepted_suggestions
    from infra.guidance_states import GitFault

    try:
        repo = repository(Path(config.WORKSPACE))
        if repo is None:
            return False
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
        oid = _git_bytes(repo, "rev-parse", "--verify", "--quiet", f"HEAD:{relative}", ok=(0, 1)).strip()
        if not oid or _git_bytes(repo, "cat-file", "blob", oid.decode()) != data:
            return False
        raw = _git_bytes(repo, "log", "-1", "--format=%ae%x00%B", "--", f":(literal){relative}").decode(errors="replace")
    except (GitFault, ValueError) as exc:  # unreadable or outside history: it proves nothing, so the version is read
        logger.info("fresh read: history of {} unreadable ({})", path.name, exc)
        return False
    email, _, message = raw.partition("\x00")
    return email.strip().endswith("@users.corsia.local") and not accepted_suggestions(message)


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
    except Exception:  # noqa: BLE001  # a failed recheck leaves the passed version shown; the next open retries
        logger.exception("fresh read: background read of {} failed", artifact_id)
    finally:
        _running.discard((artifact_id, sha))


def _start_recheck(artifact_id: str, path: Path, data: bytes, holder: str | None, node_id: str | None) -> None:
    """One background read per document and version, started from an open."""
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
    if _person_wrote(path, raw):
        record(ReadRecord(kind="document", item_ref=str(artifact["id"]), sha256=current, verdict=PERSON_EDIT,
                          documents=((str(artifact["id"]), current, raw),)))
        return None
    failed = row["recheck_sha256"] == current and row["recheck_verdict"] in ("failed", "held")
    if not failed:
        _start_recheck(str(artifact["id"]), path, raw, row["holder_thread_id"], row["node_id"])
    return {
        "text": bytes(row["body"]).decode("utf-8", errors="replace"),
        "writable": writable and failed,
        "passed_version": {"sha256": row["sha256"], "fresh_read_id": row["fresh_read_id"],
                           "current_sha256": current, "notice": NOTICE_FAILED if failed else NOTICE_WAITING},
    }
