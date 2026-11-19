"""The person's own write to a document an agent handed over: it moves the pass, with no read.

Every write whose actor is the person lands here, at the two points all of them cross: the
document writer (``doceditor.document_save.save_document_text``: editor saves, version
restores, accepted suggestions) and the live registration
(``doceditor.live_publisher.register_publication``: live typing, and restores and accepts on
an open document). Each records ``person_edit`` for exactly the bytes written, so what the
person wrote is never shown back to them as an unchecked agent version.

Two kinds of write differ when the bytes they replace never passed (an agent's newer version
waiting on, or failing, its check):

- A whole-text write through the document writer (an editor save of the passed copy, a restore
  of a document not open live) replaces those bytes. They
  are kept first, in the document's history (the save records them as an outside version), or,
  where the file has no history, in the holding agent's scratch folder; that agent is woken
  once, naming where its text is. The save is checked against the bytes it replaces, so a
  newer write landing in between is not silently overwritten.
- A patch (an accepted suggestion, any live edit) applies to those bytes, so the result may still
  hold the agent's unchecked words: it is not recorded, and a check of the result starts (from the
  writer for an accept, from the next open for a live edit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from core.fresh_read_store import PERSON_EDIT, ReadRecord, record, sha256_bytes, view
from infra.db_async import run_in_db


def _before(path: str) -> dict[str, Any] | None:
    """Blocking. The governed document at ``path``, its current bytes and view, or None."""
    from infra.database import get_db
    from registry import service as registry_service

    db = get_db()
    try:
        art = registry_service.find_file_identity(str(Path(path).expanduser()), db=db)
    finally:
        db.close()
    row = view(str(art["id"])) if art is not None else None
    if row is None:
        return None
    try:
        current = Path(art["path"] or path).read_bytes()
    except OSError:
        return None
    return {"artifact_id": str(art["id"]), "path": Path(art["path"] or path), "current": current, "view": row}


def _keep_aside(before: dict[str, Any]) -> str | None:
    """Blocking. Where the agent's replaced bytes are kept when the file records no history."""
    from infra import config
    from infra.database import get_db
    from infra.guidance_targets import history_target

    if history_target(before["path"], Path(config.WORKSPACE)) is not None:
        return None
    holder = before["view"]["holder_thread_id"]
    if not holder:
        return None
    from core.thread_lifecycle_paths import thread_dir

    db = get_db()
    try:
        row = db.execute("SELECT project_id FROM threads WHERE id=?", (holder,)).fetchone()
        root = thread_dir(db, holder, row["project_id"] if row else None, Path(config.WORKSPACE))
    finally:
        db.close()
    if root is None:
        return None
    scratch = root / "scratch" / f"{before['path'].name}-{sha256_bytes(before['current'])[:12]}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(before["current"])
    return str(scratch)


def _history_commit(path: Path, data: bytes) -> str | None:
    """Blocking. The newest recorded version holding ``data``, among the versions history lists."""
    from infra import config
    from infra.document_history import list_versions, repository, version_bytes
    from infra.guidance_states import GitFault

    try:
        repo = repository(Path(config.WORKSPACE))
        if repo is None:
            return None
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
        versions, _more = list_versions(Path(config.WORKSPACE), relative)
        for version in versions:
            commit = str(version.get("id") or "")
            if commit and version_bytes(repo, relative, commit) == data:
                return commit
    except (GitFault, ValueError) as exc:  # the wake still names the document and the hash
        logger.info("fresh read: no history version found for {} ({})", path.name, exc)
        return None
    return None


def _record(artifact_id: str, written: bytes, db=None) -> None:
    """Blocking. Move the document's pass to ``written``, the person's own bytes."""
    sha = sha256_bytes(written)
    record(ReadRecord(kind="document", item_ref=artifact_id, sha256=sha, verdict=PERSON_EDIT,
                      documents=((artifact_id, sha, written),)), db=db)


def person_published(artifact_id: str, replaced: bytes, written: bytes, *, db) -> None:
    """Blocking, inside the live registration's transaction on ``db``. A live edit by the person: a
    patch on ``replaced`` that wrote ``written``."""
    row = view(artifact_id, db=db)
    if row is None:
        return
    if sha256_bytes(replaced) != row["sha256"]:
        # Patched onto bytes that never passed; the next open reads the result.
        return
    _record(artifact_id, written, db=db)


async def person_save(path: str, written: bytes, save: Callable[[str | None], Awaitable[dict]], *,
                      patch: bool = False) -> dict:
    """Run the person's ``save`` of ``written`` (given the sha it must replace, if any) and move the pass."""
    before = await run_in_db(_before, path)
    if before is None:
        return await save(None)
    replaced = sha256_bytes(before["current"])
    stale = replaced != before["view"]["sha256"]
    if patch and stale:
        result = await save(None)
        from core.fresh_read_view import start_recheck

        start_recheck(before["artifact_id"], before["path"], written, before["view"]["holder_thread_id"],
                      before["view"]["node_id"])
        return result
    scratch = await run_in_db(_keep_aside, before) if stale else None
    result = await save(replaced if stale else None)
    await run_in_db(_record, before["artifact_id"], written)
    holder = before["view"]["holder_thread_id"]
    if stale and holder:
        await _wake_holder(holder, before, replaced, scratch)
    return result


async def _wake_holder(holder: str, before: dict[str, Any], replaced: str, scratch: str | None) -> None:
    from events.delivery import deliver_to_thread

    commit = None if scratch else await run_in_db(_history_commit, before["path"], before["current"])
    where = (f"its history keeps your version as {commit[:12]}" if commit else
             f"your version is at {scratch}" if scratch else "its history keeps your version")
    summary = (f"The person saved over {before['path'].name} after your newer version did not pass its check; "
               f"{where}.")
    try:
        await deliver_to_thread(holder, "fresh_read.saved_over", summary,
                                {"artifact_id": before["artifact_id"], "path": str(before["path"]),
                                 "replaced_sha256": replaced, "history_commit": commit, "scratch_path": scratch},
                                delivery_id=f"fresh-read-saved-over:{before['artifact_id']}:{replaced}")
    except Exception:  # noqa: BLE001  # the save landed; a missed wake is logged, never undone
        logger.exception("fresh read: waking {} after a person's save failed", holder)
