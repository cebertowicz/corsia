"""Validated document save domain logic shared by HTTP and agent adapters."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.skill_validation import is_skill_file
from core.workspace_context_sources import is_workspace_context_path
from doceditor.comment_actions import DocumentWriter
from doceditor.document_scope import (
    MAX_TEXT_BYTES,
    UI_THREAD,
    ProjectNotFound,
    path_write_allowed,
    revision,
    scope_bases,
)
from infra import config
from infra.auth import owns_thread
from infra.database import get_db
from infra.db_async import run_in_db, run_off_db
from infra.guidance_git import save_guidance
from infra.guidance_targets import GuidanceTarget, guidance_target
from infra.guidance_messages import saver_author
from registry import service as registry_service
from registry.durable_writes import write_text
from registry.shared_write_errors import DurableWritePolicyError
from registry.shared_document_events import report_plain_write
from tools.file_write_completion import record_context_source_change

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentSave:
    path: str
    text: str
    description: str | None = None
    project_id: str | None = None
    thread_id: str | None = None
    create: bool = False


class DocumentSaveError(ValueError):
    """A domain refusal mapped by each transport adapter."""

    def __init__(self, kind: str, message: str, detail: Any = None) -> None:
        self.kind = kind
        self.message = message
        self.detail = detail
        super().__init__(message)


def _invalid_skill_save_error(errors: tuple[str, ...], line: str) -> DocumentSaveError:
    """Carry the canonical skill refusal through document-save adapters unchanged."""
    return DocumentSaveError(
        "invalid_skill",
        line,
        {"message": "Invalid SKILL.md", "errors": list(errors), "line": line},
    )


def _refuse_invalid_skill(path: Path, text: str) -> None:
    """Refuse a SKILL.md that would not load, before any byte is written; other files pass."""
    if not is_skill_file(path):
        return
    from infra.skill_save_validation import invalid_skill_write

    if (refusal := invalid_skill_write({path: text.encode()})) is not None:
        raise _invalid_skill_save_error(refusal.errors, refusal.line)


class FileChangedOutside(DocumentSaveError):
    """The file no longer holds the bytes a live write was built on; nothing was replaced."""

    def __init__(self) -> None:
        super().__init__("stale", "The document changed outside CORSIA while this write was saving."
                                  " Nothing was replaced; try again.")


def _saved_payload(resolved: Path, actor_thread_id: str, extra: dict) -> dict:
    """Build the editor's save receipt after bytes and registration landed.

    The context-source notice is the same owner the agent file tools use. On
    failure the save stays successful and the receipt carries the failure text
    so the client can show it; nothing retries.
    """
    notice_error = record_context_source_change(
        resolved, thread_id=actor_thread_id, workspace=config.WORKSPACE
    )
    stat = resolved.stat()
    payload = {
        **extra,
        "saved": True,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "revision": revision(resolved.read_bytes()),
    }
    if notice_error is not None:
        payload["context_notice_error"] = notice_error
    return payload


async def _save_validated(
    path: Path, text: str, author: Callable[[], str], reason: str | None, write,
) -> dict:
    """Write a validated editor save. Guidance goes through history off the DB executor, whose
    workers its lock wait and sweep would otherwise hold (issue #2272)."""
    target = await run_in_db(guidance_target, path, Path(config.WORKSPACE))
    if target is None:
        _refuse_invalid_skill(path, text)
        return await run_in_db(write, path)
    return await run_off_db(_guided_write, path, target, text, author, reason, write)


def _guided_write(
    path: Path, target: GuidanceTarget, text: str, author: Callable[[], str], reason: str | None, write,
) -> dict:
    """Blocking. Run a validated editor save of a guidance file through guidance history (issue #2272)."""
    payloads: list[dict] = []
    saved = save_guidance(
        {path: text.encode()}, workspace=Path(config.WORKSPACE), target=target,
        author=author(), reason=reason, write=lambda _path, _data: payloads.append(write(path)),
    )
    if saved.outcome == "refused":
        assert saved.line is not None
        raise _invalid_skill_save_error(saved.errors, saved.line)
    if not payloads:
        raise DocumentSaveError("save_failed", saved.line or "Document was not saved")
    if saved.line is not None:
        payloads[0]["guidance_history"] = saved.line
    return payloads[0]


def _validate_document_write(body: DocumentSave, user_id: str) -> Path:
    """Blocking. Every rejection comes before any write or Git side effect."""
    db = get_db()
    try:
        if body.thread_id and not owns_thread(body.thread_id, user_id, db=db):
            raise DocumentSaveError("thread_not_found", "Thread not found")
        try:
            bases = scope_bases(body.project_id, db)
        except ProjectNotFound as exc:
            raise DocumentSaveError("project_not_found", str(exc)) from exc
        path = Path(body.path).expanduser()
        try:
            resolved = path.resolve(strict=not body.create)
        except OSError as exc:
            raise DocumentSaveError("no_such_file", f"No such file: {exc}") from exc
        # Workspace context files are injected into every system prompt.
        # They remain read-only through the document API so this endpoint
        # cannot become a system-prompt-injection vector.
        if is_workspace_context_path(resolved, config.WORKSPACE, db=db):
            raise DocumentSaveError(
                "context_read_only", "Workspace context file is read-only via the doc API"
            )
        if not path_write_allowed(resolved, bases):
            raise DocumentSaveError("outside_scope", "Path is outside this scope's roots")
    finally:
        db.close()
    if len(body.text.encode()) > MAX_TEXT_BYTES:
        raise DocumentSaveError("too_large", "Document too large")
    if body.create:
        if resolved.exists():
            raise DocumentSaveError("already_exists", "File already exists")
    elif not resolved.is_file():
        raise DocumentSaveError("not_a_file", "Not a file")
    return resolved


def validate_live_write(path: Path, text: str, actor) -> Path:
    """Validate each live operation with its server-issued caller context."""
    if actor is None:
        raise DocumentSaveError("thread_not_found", "Live writes require a current actor")
    user_id = actor.user_id
    thread_id = None if actor.thread_id == UI_THREAD else actor.thread_id
    if user_id is None and thread_id is not None:
        db = get_db()
        try:
            row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
            user_id = row[0] if row else None
        finally:
            db.close()
    if user_id is None:
        raise DocumentSaveError("thread_not_found", "Thread not found")
    resolved = _validate_document_write(DocumentSave(
        str(path), text, description=actor.description, project_id=actor.project_id,
        thread_id=thread_id,
    ), user_id)
    # The live lane renames bytes itself, so the SKILL.md check the HTTP save runs in
    # _save_validated must run here, before the rename (a guidance file never goes live).
    _refuse_invalid_skill(resolved, text)
    return resolved


def write_atomic_plain(path: Path, text: str, prior_sha256: str | None = None) -> None:
    """The lane's non-guidance rename, with no application-DB transaction.

    Validation and registration stay with the document save owner. The stage
    name is deterministic so recovery can remove it after a killed process.
    With ``prior_sha256``, the file is compared with it immediately before the
    rename, in this same synchronous call, and a changed file is never replaced.
    """
    from tools.filesystem_effects import (
        bound_file_mode, replace_bound_file, stage_bound_bytes, unlink_bound_file,
    )

    leaf = f".corsia-live-{path.name}.stage"
    stage = path.with_name(leaf)
    unlink_bound_file(stage, missing_ok=True)
    try:
        stage_bound_bytes(path, text.encode("utf-8"), leaf=leaf, mode=bound_file_mode(path))
        if prior_sha256 is not None and hashlib.sha256(path.read_bytes()).hexdigest() != prior_sha256:
            raise FileChangedOutside()
        replace_bound_file(stage, path)
    finally:
        unlink_bound_file(stage, missing_ok=True)


def _write_document(
    body: DocumentSave, resolved: Path, direct_user: bool, expected_sha256: str | None = None,
) -> dict:
    """Blocking. Write a validated document and resolve its artifact identity."""
    if expected_sha256 is not None:
        current_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if current_sha256 != expected_sha256:
            raise DocumentSaveError(
                "stale",
                "The document changed while these suggestions were being applied. Nothing was changed; try again.",
            )
    try:
        decision = write_text(
            resolved,
            body.text,
            thread_id=body.thread_id or UI_THREAD,
            project_id=body.project_id,
            workspace=config.WORKSPACE,
            description=body.description,
            direct_user=direct_user,
            notify=False,
        )
    except DurableWritePolicyError as exc:
        raise DocumentSaveError("durable_write_policy", str(exc)) from exc
    actor = body.thread_id or UI_THREAD
    if isinstance(decision, dict):
        return _saved_payload(resolved, actor, {**decision, "_notification": decision["artifact"]})
    if body.create:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    reaches_owner = decision is not None and decision.reaches_owner
    before = resolved.read_bytes() if reaches_owner or (not body.thread_id and resolved.is_file()) else None
    # Only the thread-less save compares against the file's prior bytes; a threaded
    # save compares against its registration's last_written_sha256.
    prior_sha256 = hashlib.sha256(before).hexdigest() if before is not None and not body.thread_id else None
    resolved.write_text(body.text, encoding="utf-8")
    new_sha256 = hashlib.sha256(body.text.encode("utf-8")).hexdigest()
    db = get_db()
    try:
        artifact_type = registry_service.classify(resolved)
        if body.thread_id:
            # The thread registers the write; the edit is the user's unless an agent made it.
            artifact = registry_service.register_write_strict(
                body.thread_id, str(resolved), metadata={"last_written_sha256": new_sha256},
                actor_thread_id=UI_THREAD if direct_user else None, db=db,
            )
        else:
            artifact = registry_service.resolve_file_identity(str(resolved), db=db)
            if artifact is not None:
                from registry.review_edit_journal import journal_reviewed_edit

                journal_reviewed_edit(
                    db, artifact, previous_sha256=prior_sha256,
                    new_sha256=new_sha256, actor_thread_id=UI_THREAD,
                )
        try:
            # The bytes are saved and recorded: a report failure never fails the save.
            report_plain_write(decision, resolved, before, thread_id=actor, db=db)
        except Exception as exc:
            _log.warning("owner report failed for saved artifact %s: %s", decision.artifact_id, type(exc).__name__)
    finally:
        db.close()
    notification = artifact or {
        "integration": "workspace",
        "type": artifact_type,
        "external_id": str(resolved),
    }
    return _saved_payload(resolved, actor, {"artifact": artifact, "_notification": notification})


async def save_document_text(
    body: DocumentSave, user_id: str, *, by_agent: bool = False, reason: str | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Shared validated document writer for editor saves and accepted suggestions.

    ``by_agent`` saves as ``body.thread_id``, not as the user: guidance history is authored
    by that thread, and the scope-structure rules apply as they do to its file tools.
    """
    resolved = await run_in_db(lambda: _validate_document_write(body, user_id))
    from doceditor.live_admission import falls_back_to_plain
    from doceditor.live_runtime import get_documents
    from doceditor.live_tools import find_live
    manager = get_documents()
    if manager is None or await run_off_db(guidance_target, resolved, config.WORKSPACE) is not None:
        return await _save_legacy_document(body, resolved, user_id, by_agent, reason, expected_sha256)
    async with manager.admission_lock(resolved):
        if (entry := find_live(resolved)) is not None:
            from doceditor import live_events
            live_events.emit("legacy_save_refused", artifact_id=entry.artifact_id, code="document_live")
            message = "This document is being edited live. Reload to keep editing; your text is still in this tab."
            raise DocumentSaveError("document_live", message, {"code": "document_live", "message": message})
        refused = manager.refused_stores.get(resolved)
        if refused is not None and not falls_back_to_plain(refused["code"]):
            # The file must not move away from acknowledged text the store still holds.
            raise DocumentSaveError(refused["code"], refused["message"], dict(refused))
        return await _save_legacy_document(body, resolved, user_id, by_agent, reason, expected_sha256)


async def _save_legacy_document(
    body: DocumentSave, resolved: Path, user_id: str, by_agent: bool,
    reason: str | None, expected_sha256: str | None,
) -> dict:
    """Keep the validated legacy writer intact under the admission boundary."""
    thread_id = body.thread_id if by_agent else None
    result = await _save_validated(
        resolved,
        body.text,
        lambda: saver_author(thread_id=thread_id) if thread_id else saver_author(user_id=user_id),
        reason,
        lambda path: _write_document(
            body, path, direct_user=not by_agent, expected_sha256=expected_sha256,
        ),
    )
    notification = result.pop("_notification")
    if notification is not None:
        registry_service.notify_changed(notification)
    return result


def agent_document_writer(thread_id: str, user_id: str) -> DocumentWriter:
    """Writer for a suggestion an agent accepts: saves as that thread, raising DocumentSaveError if refused."""
    async def write(
        path: str, text: str, project_id: str | None, reason: str, expected_sha256: str | None,
    ) -> dict:
        return await save_document_text(
            DocumentSave(path=path, text=text, thread_id=thread_id, project_id=project_id),
            user_id,
            by_agent=True,
            reason=reason,
            expected_sha256=expected_sha256,
        )
    return write
