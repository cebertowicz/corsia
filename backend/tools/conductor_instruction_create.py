"""Revisioned create/attach companion to conductor_instruction_files."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from core.agent_roster_mutations import RosterChangeError, require_designated_conductor_target
from core.bootloader_management import thread_bootloader_files
from core.workspace_layout import resolve_project_root, resolve_thread_root, resolve_workspace_root
from infra import config
from infra.db_async import run_in_db
from tools.base import Tool
from tools.conductor_instruction_files import _actor_user_id, _instruction_paths
from tools.file_write_completion import GUIDANCE_PARAMETERS

MAX_INSTALL_BYTES = 32_768

logger = logging.getLogger(__name__)


def _bundle_revision(writes: Mapping[Path, bytes | None]) -> str:
    digest = hashlib.sha256()
    for path, content in sorted(writes.items()):
        digest.update(str(path).encode() + b"\0" + (content or b"") + b"\0")
    return digest.hexdigest()


def _undelivered_error(kind: str) -> str:
    """Explain why the saved instruction did not reach the target's next turn."""
    if kind == "responsibility":
        return (
            "Saved but not delivered: the bootloader attachment was not included; "
            "read the destination before retrying"
        )
    return (
        "Saved but not delivered: a skill is shadowed or invalid; "
        "read the destination before retrying"
    )


def _install_paths(target, kind: str, name: str | None, content: str, supporting_files: dict) -> dict[Path, bytes]:
    from core.skill_validation import validate_skill_dir_name, validate_skill_text

    workspace = resolve_workspace_root(config.WORKSPACE)
    scope_root = workspace
    if target["project_id"]:
        from infra.database import get_db

        db = get_db()
        try:
            project_root = resolve_project_root(workspace, target["project_id"], db=db)
        finally:
            db.close()
        if project_root is None:
            raise RosterChangeError("target project context root is unavailable")
        scope_root = project_root
    tray = resolve_thread_root(scope_root, target["id"])
    if not tray.resolve().is_relative_to(scope_root.resolve()):
        raise RosterChangeError("target thread tray escapes its scope root")
    if kind == "responsibility":
        if name or supporting_files:
            raise RosterChangeError("responsibility uses the fixed RESPONSIBILITY.md path; omit name and supporting_files")
        root = tray / "context"
        writes = {root / "RESPONSIBILITY.md": content.encode()}
    elif kind == "skill":
        if not name:
            raise RosterChangeError("skill name is required")
        try:
            validate_skill_dir_name(name)
        except ValueError as exc:
            raise RosterChangeError("skill name must be lowercase letters, numbers and hyphens") from exc
        valid = validate_skill_text(content)
        if not valid.ok or valid.name != name:
            raise RosterChangeError("SKILL.md requires matching name, description and valid corsia frontmatter")
        root = tray / "skills" / name
        writes = {root / "SKILL.md": content.encode()}
        for relative, text in supporting_files.items():
            if not re.fullmatch(r"(?:scripts|references)/[a-zA-Z0-9_.-]+", relative) or not isinstance(text, str):
                raise RosterChangeError("supporting_files paths must be one-level scripts/ or references/ files with text content")
            writes[root / relative] = text.encode()
    else:
        raise RosterChangeError("kind must be responsibility or skill")
    for path in writes:
        if not path.resolve().is_relative_to(tray.resolve()) or path.is_symlink():
            raise RosterChangeError("instruction destination escapes the target thread tray")
    if sum(map(len, writes.values())) > MAX_INSTALL_BYTES:
        raise RosterChangeError("instruction bundle exceeds 32768 bytes; reduce content")
    return writes


def _save_bundle(writes: dict[Path, bytes], before: dict[Path, bytes | None], actor: str, kwargs: dict) -> dict:
    from infra.guidance_git import GuidanceTarget, digest, save_guidance
    from infra.guidance_messages import saver_author
    from infra.skill_save_validation import REFUSED

    saved = save_guidance(
        writes, workspace=Path(config.WORKSPACE), target=GuidanceTarget("thread"),
        author=saver_author(thread_id=actor), reason=kwargs.get("reason"), evidence=kwargs.get("evidence"),
        expected={path: digest(raw.decode()) if raw is not None else None for path, raw in before.items()},
    )
    if saved.outcome == REFUSED:
        raise RosterChangeError(saved.line or "SKILL.md was refused")
    if saved.outcome == "stale":
        raise RosterChangeError("instruction file changed during save; read current files and retry")
    if any(not path.is_file() or path.read_bytes() != raw for path, raw in writes.items()):
        raise RosterChangeError("instruction bytes were not saved; inspect target files before retrying")
    return {"guidance_history": saved.line, "history_outcome": saved.outcome}


def _install(actor: str, target_id: str, kind: str, name: str | None, content: str,
             expected_revision: str, supporting_files: dict, kwargs: dict) -> dict:
    from api.context_bootloader import BootloaderTarget, _write_context_change
    from core.skills_registry import resolve_effective_skills
    from infra.database import write_transaction

    with write_transaction(None, scope="coordination_instruction_file_create") as db:
        user_id = _actor_user_id(db, actor)
        target = require_designated_conductor_target(target_id, user_id=user_id, actor_thread_id=actor, db=db)
        writes = _install_paths(target, kind, name, content, supporting_files)
        before = {path: path.read_bytes() if path.exists() else None for path in writes}
        current_revision = "absent" if all(value is None for value in before.values()) else _bundle_revision(before)
        changed = before != writes
        if changed and expected_revision != current_revision:
            raise RosterChangeError(f"instruction bundle changed; current revision is {current_revision}; read current files and retry")
    # The guidance save closes its own pooled connection, which rolls back this thread's open
    # transaction, so it runs between the check and the attach, as the replace tool's save does.
    history = _save_bundle(writes, before, actor, kwargs) if changed else {}
    primary = next(iter(writes))
    with write_transaction(None, scope="conductor_instruction_file_attach") as db:
        if kind == "responsibility":
            files = thread_bootloader_files(target_id, db=db)
            attached = str(primary.resolve()) in _instruction_paths(target, db)
            if not attached:
                _write_context_change(
                    db, target=BootloaderTarget("thread", target_id, primary.parent, (primary.parent,), True),
                    user_id=user_id, action="attach", files=[*files, str(primary)], expected_files=files,
                )
            delivered = str(primary.resolve()) in _instruction_paths(target, db)
        else:
            skills = resolve_effective_skills(
                thread_id=target_id, project_id=target["project_id"], workspace=Path(config.WORKSPACE), db=db,
            )
            from core.lesson_index import partition

            offered = partition(skills.included, skills.omitted, thread_id=target_id, db=db)
            delivered = any(row.path.resolve() == primary.resolve() for row in offered.offered)
            attached = delivered
        if kind == "skill" and changed:
            from core.bootloader_management import log_bootloader_event

            log_bootloader_event(db, actor_user_id=user_id, actor_thread_id=actor,
                                 scope="thread", target_id=target_id, action="install_skill",
                                 files=[str(path) for path in writes])
        return {
            "ok": delivered, "thread_id": target_id, "kind": kind, "path": str(primary),
            "revision": _bundle_revision(writes), "changed": changed or not attached,
            "delivery": {"next_turn": delivered, "surface": "bootloader" if kind == "responsibility" else "skills-list"},
            "supporting_files": [str(path) for path in writes if path != primary],
            **history,
            "error": None if delivered else _undelivered_error(kind),
        }


class ConductorInstructionFileCreateTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "coordination_instruction_file_create"

    @property
    def description(self) -> str:
        return (
            "Load skill conductor-api for setup. For the designated same-scope conductor: create and attach a target thread's "
            "RESPONSIBILITY.md, or install its thread skill and optional scripts/references. "
            "Use expected_revision='absent' for creation; copy the returned bundle revision "
            "for later edits. Same desired state retries are idempotent. Returns verified next-turn "
            "bootloader or skills-list delivery; support scripts are available, never executed. "
            "Use only for user-authorized responsibilities and guidance."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "required": ["target_thread_id", "kind", "expected_revision", "content"],
            "properties": {
                "target_thread_id": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["responsibility", "skill"]},
                "expected_revision": {"type": "string", "pattern": "^(absent|[0-9a-f]{64})$"},
                "content": {"type": "string", "minLength": 1, "maxLength": MAX_INSTALL_BYTES},
                "name": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                "supporting_files": {"type": "object", "additionalProperties": {"type": "string"}},
                **GUIDANCE_PARAMETERS,
            },
        }

    async def execute(self, target_thread_id: str, kind: str, expected_revision: str,
                      content: str, name: str | None = None, supporting_files: dict | None = None,
                      **kwargs: Any) -> str:
        actor = kwargs.get("_thread_id")
        if not isinstance(actor, str) or not actor:
            return "Error: current thread identity is unavailable; retry from a live conductor turn."
        if not re.fullmatch(r"absent|[0-9a-f]{64}", expected_revision) or not content.strip():
            return "Error: use expected_revision='absent' or the returned bundle revision, and nonempty content."
        try:
            receipt = await run_in_db(_install, actor, target_thread_id, kind, name, content,
                                      expected_revision, supporting_files or {}, kwargs)
            if receipt["changed"]:
                from tools.file_write_completion import record_context_source_change

                notice = record_context_source_change(Path(receipt["path"]), thread_id=actor, workspace=config.WORKSPACE)
                if notice is not None:
                    receipt["context_notice_error"] = notice
            return json.dumps(receipt, sort_keys=True)
        except RosterChangeError as exc:
            return f"Error: {exc}."
        except Exception:
            logger.exception("Conductor instruction install failed")
            return "Error: instruction install failed; read the destination before retrying because files may already be saved."
