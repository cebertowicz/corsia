"""Conductor-only reads and revision-checked writes for attached instructions."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from doceditor.document_scope import MAX_TEXT_BYTES
from core.agent_roster_mutations import (
    RosterChangeError,
    require_designated_conductor_target,
)
from core.bootloader_management import log_bootloader_event, thread_bootloader_files
from core.workspace_layout import (
    resolve_project_root,
    resolve_thread_root,
    resolve_workspace_root,
)
from infra import config
from infra.db_async import run_in_db, run_off_db
from tools.base import Tool
from tools.file_write_completion import GUIDANCE_PARAMETERS

log = logging.getLogger(__name__)

_REVISION = re.compile(r"[0-9a-f]{64}")


def _revision(content: bytes) -> str:
    """Return the opaque content revision used for instruction-file writes."""
    return hashlib.sha256(content).hexdigest()


def _actor_user_id(db, actor_thread_id: str) -> str:
    """Resolve the server-bound actor user without accepting a caller parameter."""
    actor = db.execute("SELECT user_id FROM threads WHERE id=?", (actor_thread_id,)).fetchone()
    if actor is None:
        raise RosterChangeError("Current thread no longer exists")
    return actor["user_id"]


def _instruction_paths(target: Any, db) -> dict[str, Path]:
    """Return target-owned, attached context files keyed by their canonical paths."""
    workspace = resolve_workspace_root(config.WORKSPACE)
    project_id = target["project_id"]
    scope_root = workspace
    if project_id:
        scope_root = resolve_project_root(workspace, str(project_id), db=db)
        if scope_root is None:
            raise RosterChangeError("target project context root is unavailable")
    root = resolve_thread_root(scope_root, str(target["id"])) / "context"
    resolved_root = root.resolve(strict=False)
    paths: dict[str, Path] = {}
    for entry in thread_bootloader_files(str(target["id"]), db=db):
        candidate = Path(entry).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=False)
        if candidate.suffix not in {".md", ".txt"} or not candidate.is_relative_to(resolved_root):
            continue
        paths[str(candidate)] = candidate
    return paths


def _target_path(paths: dict[str, Path], path: str) -> Path:
    """Resolve an advertised attached path, refusing unlisted paths."""
    try:
        candidate = str(Path(path).expanduser().resolve(strict=False))
    except OSError as exc:
        raise RosterChangeError("instruction file path is invalid") from exc
    target = paths.get(candidate)
    if target is None:
        raise RosterChangeError(
            "instruction file is not attached; call conductor_instruction_file_read "
            "without path and copy one returned path"
        )
    return target


def _readable_record(path: Path) -> dict[str, Any] | None:
    """Describe one readable instruction file without returning its content."""
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        return None
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return {"path": str(path), "size": size, "revision": _revision(raw)}


class ConductorInstructionFileReadTool(Tool):
    """List or read one attached target instruction file with its revision."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "conductor_instruction_file_read"

    @property
    def description(self) -> str:
        return (
            "For the designated conductor only: omit path to list a target thread's "
            "attached instruction files and revisions, or provide one returned path to "
            "read its content. Use its revision with conductor_instruction_file_replace."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["target_thread_id"],
            "properties": {
                "target_thread_id": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
            },
        }

    async def execute(self, target_thread_id: str, path: str | None = None, **kwargs: Any) -> str:
        actor_thread_id = kwargs.get("_thread_id")
        if not isinstance(actor_thread_id, str) or not actor_thread_id:
            return "Error: current thread identity is unavailable; retry from a live conductor turn."

        def _read() -> dict[str, Any]:
            from infra.database import get_db

            db = get_db()
            try:
                user_id = _actor_user_id(db, actor_thread_id)
                target = require_designated_conductor_target(
                    target_thread_id, user_id=user_id, actor_thread_id=actor_thread_id, db=db,
                )
                paths = _instruction_paths(target, db)
                if path is None:
                    files = [record for item in paths.values() if (record := _readable_record(item))]
                    return {"thread_id": target_thread_id, "files": files}
                file_path = _target_path(paths, path)
                if not file_path.is_file():
                    raise RosterChangeError("attached instruction file is missing")
                raw = file_path.read_bytes()
                if len(raw) > MAX_TEXT_BYTES:
                    raise RosterChangeError(f"attached instruction file exceeds the {MAX_TEXT_BYTES}-byte limit")
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RosterChangeError("attached instruction file is not valid UTF-8 text") from exc
                return {
                    "thread_id": target_thread_id,
                    "path": str(file_path),
                    "content": content,
                    "revision": _revision(raw),
                    "size": len(raw),
                }
            finally:
                db.close()

        return await _run_instruction_operation(_read)


class ConductorInstructionFileReplaceTool(Tool):
    """Replace one attached target instruction file after a revision check."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "conductor_instruction_file_replace"

    @property
    def description(self) -> str:
        return (
            "For the designated conductor only: replace one existing attached instruction "
            "file after reading it with conductor_instruction_file_read. Copy the returned "
            "path and SHA-256 revision exactly. This cannot create, attach, move, delete, "
            "or change an instruction file's mission or authority."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["target_thread_id", "path", "expected_revision", "content"],
            "properties": {
                "target_thread_id": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
                "expected_revision": {
                    "type": "string", "pattern": "^[0-9a-f]{64}$",
                    "description": "Copy the revision returned by conductor_instruction_file_read.",
                },
                "content": {
                    "type": "string", "maxLength": MAX_TEXT_BYTES,
                    "description": f"Replacement text; its UTF-8 encoding must not exceed {MAX_TEXT_BYTES} bytes.",
                },
                **GUIDANCE_PARAMETERS,
            },
        }

    async def execute(
        self, target_thread_id: str, path: str, expected_revision: str, content: str, **kwargs: Any,
    ) -> str:
        actor_thread_id = kwargs.get("_thread_id")
        if not isinstance(actor_thread_id, str) or not actor_thread_id:
            return "Error: current thread identity is unavailable; retry from a live conductor turn."
        if not isinstance(expected_revision, str) or not _REVISION.fullmatch(expected_revision):
            return "Error: expected_revision must be the 64-character lowercase SHA-256 revision from conductor_instruction_file_read."
        if not isinstance(content, str):
            return "Error: content must be replacement text."
        replacement = content.encode("utf-8")
        if len(replacement) > MAX_TEXT_BYTES:
            return f"Error: replacement content exceeds the {MAX_TEXT_BYTES}-byte instruction-file limit; shorten content and retry."

        def _locate() -> dict[str, Any]:
            from infra.database import get_db

            db = get_db()
            try:
                user_id = _actor_user_id(db, actor_thread_id)
                target = require_designated_conductor_target(
                    target_thread_id, user_id=user_id, actor_thread_id=actor_thread_id, db=db,
                )
                file_path = _target_path(_instruction_paths(target, db), path)
                if not file_path.is_file():
                    raise RosterChangeError("attached instruction file is missing")
                if file_path.stat().st_size > MAX_TEXT_BYTES:
                    raise RosterChangeError(f"attached instruction file exceeds the {MAX_TEXT_BYTES}-byte limit")
                return {"path": str(file_path), "user_id": user_id}
            finally:
                db.close()

        located = await _run_instruction_operation(_locate)
        if located.startswith("Error:"):
            return located
        file_path, user_id = Path(json.loads(located)["path"]), json.loads(located)["user_id"]
        saved = await run_off_db(_save, file_path, replacement, expected_revision, actor_thread_id, kwargs)
        if saved.outcome == "refused":
            return saved.line or "Error: instruction file was refused."
        if saved.outcome == "stale":
            return (
                "Error: instruction file changed since it was read; call "
                "conductor_instruction_file_read again, merge, and retry once."
            )
        receipt: dict[str, Any] = {
            "ok": True, "thread_id": target_thread_id, "path": str(file_path),
            "before_revision": expected_revision, "revision": _revision(replacement), "size": len(replacement),
        }
        if saved.line:
            receipt["guidance_history"] = saved.line
        audit_error = await run_in_db(_audit, user_id, actor_thread_id, target_thread_id, receipt)
        if audit_error is not None:
            receipt["audit_error"] = audit_error
        from tools.file_write_completion import record_context_source_change

        notice_error = record_context_source_change(file_path, thread_id=actor_thread_id, workspace=config.WORKSPACE)
        if notice_error is not None:
            receipt["context_notice_error"] = notice_error
        return json.dumps(receipt, sort_keys=True)


def _save(path: Path, replacement: bytes, expected_revision: str, actor_thread_id: str, kwargs: dict):
    """Blocking. Save through the one guidance-history owner, which rechecks the read
    revision under its lock, so a concurrent guidance save is never overwritten."""
    from infra.guidance_git import GuidanceTarget, guidance_target, save_guidance
    from infra.guidance_messages import saver_author

    target = guidance_target(path) or GuidanceTarget(
        "unknown", "this attached instruction file is not classified as guidance",
    )
    return save_guidance(
        {path: replacement}, workspace=Path(config.WORKSPACE), target=target,
        author=saver_author(thread_id=actor_thread_id), reason=kwargs.get("reason"),
        evidence=kwargs.get("evidence"), expected={path: expected_revision},
    )


def _audit(user_id: str, actor_thread_id: str, target_thread_id: str, receipt: dict[str, Any]) -> str | None:
    """Record the replace; the file is already saved, so a failure is reported, never raised."""
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="conductor_instruction_file_replace") as connection:
            log_bootloader_event(
                connection, actor_user_id=user_id, actor_thread_id=actor_thread_id, scope="thread",
                target_id=target_thread_id, action="replace_content",
                files=[{
                    "path": receipt["path"], "before_revision": receipt["before_revision"],
                    "after_revision": receipt["revision"],
                }],
            )
        return None
    except Exception as exc:
        log.exception("instruction file audit failed: %s", receipt["path"])
        return f"Saved, but the audit row was not recorded: {exc}. Report this."
    finally:
        db.close()


async def _run_instruction_operation(operation) -> str:
    """Return a self-explanatory native-tool response for one guarded operation."""
    try:
        return json.dumps(await run_in_db(operation), sort_keys=True)
    except RosterChangeError as exc:
        return f"Error: {exc}."
