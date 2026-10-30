"""Read and write file tools."""

import asyncio
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any

from host_access.credential_sanitizer import CredentialSafeText, sanitize_credential_text
from host_access.source_redaction import sanitize_source
from infra.constants import READ_FILE_OUTPUT_CHAR_CAP
from infra.db_async import run_in_db, run_off_db
from infra.guidance_targets import guidance_target
from tools.base import Tool
from tools.filesystem_binary import open_bound_binary
from tools.file_write_completion import (
    GUIDANCE_PARAMETERS,
    announce_published_write,
    managed_write,
    save_guided_write_and_announce,
)
from tools.filesystem_gate import _resolve_path_for_action
from doceditor.live_tools import record_read_base, write_live

_WRITE_SUCCESS = re.compile(
    r"^Successfully wrote [0-9]+ bytes to (?P<path>.+)$",
    re.DOTALL,
)


def write_success_result(path: Path, size: int) -> str:
    """Return the canonical native write receipt consumed by the observer."""
    return f"Successfully wrote {size} bytes to {path}"


def write_success_path(result: str) -> Path | None:
    """Read the effective absolute path from a canonical native write receipt."""
    match = _WRITE_SUCCESS.fullmatch(result)
    if match is None:
        return None
    path = Path(match.group("path")).expanduser()
    return path if path.is_absolute() else None


@dataclass(frozen=True)
class _ReadOutcome:
    """What one read returned, plus the bytes it came from."""

    text: str
    data: bytes | None = None
    whole: bool = False


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_file"

    OUTPUT_CHAR_CAP = READ_FILE_OUTPUT_CHAR_CAP

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file at the given path.\n\n"
            "By default reads the entire file. For large files, use offset and limit to read "
            "specific line ranges (e.g., offset=100, limit=50 reads lines 100-149).\n\n"
            "Output includes line numbers for easy reference.\n"
            "Output is capped at ~30K characters. For large files, use offset/limit to read sections.\n\n"
            "USE THIS when you need to:\n"
            "- Examine file contents before modifying them\n"
            "- Check configuration files, code, or documentation\n"
            "- Verify the result of a previous edit\n"
            "- Read a specific section of a large file\n\n"
            "IMPORTANT: Always read a file before editing it to confirm its current content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to read (absolute or relative to workspace)"
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-based). Only use for large files.",
                    "minimum": 1
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read. Only use for large files.",
                    "minimum": 1
                },
            },
            "required": ["path"]
        }

    _MAX_READ_BYTES = 10 * 1024 * 1024

    def _read_authorized_file(
        self,
        file_path: Path,
        offset: int | None,
        limit: int | None,
    ) -> _ReadOutcome:
        """Read one authorized path through a single bound descriptor.

        Runs off the event loop: digesting media can touch hundreds of
        megabytes. One bound open serves both the media check and the text
        read, so the authorized object is never re-resolved between them.
        """
        from core.media_reads import read_media_result

        with open_bound_binary(file_path) as (handle, file_stat):
            media = read_media_result(handle, file_stat, file_path)
            if media is not None:
                return _ReadOutcome(media)
            size = file_stat.st_size
            if size > self._MAX_READ_BYTES:
                size_mb = size / (1024 * 1024)
                return _ReadOutcome(f"Error: File too large ({size_mb:.0f}MB). Max readable size is {self._MAX_READ_BYTES // (1024*1024)}MB. Use offset/limit for partial reads, or a different tool.")
            data = handle.read()
        content = data.decode("utf-8")
        source_safe = sanitize_source(content, file_path.suffix)
        text, whole = self._render_lines(
            source_safe if source_safe is not None else content, offset, limit,
        )
        return _ReadOutcome(
            CredentialSafeText(text) if source_safe is not None else text, data, whole,
        )

    def _render_lines(
        self,
        content: str,
        offset: int | None,
        limit: int | None,
    ) -> tuple[str, bool]:
        """Number and bound the selected line window for the model.

        Also returns whether that window is the whole file.
        """
        lines = content.splitlines()
        total_lines = len(lines)

        start = (offset - 1) if offset else 0
        end = (start + limit) if limit else total_lines

        start = max(0, min(start, total_lines))
        end = max(start, min(end, total_lines))

        selected = lines[start:end]
        whole = start == 0 and end == total_lines

        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:>6}\t{line}")

        result = "\n".join(numbered)

        if offset or limit:
            if end < total_lines:
                result += f"\n\n... ({total_lines - end} more lines. Use offset={end + 1} to continue.)"
        elif total_lines > 2000:
            whole = False
            numbered = numbered[:2000]
            result = "\n".join(numbered)
            result += f"\n\n... (truncated at 2000 lines. Total: {total_lines} lines. Use offset/limit to read specific sections.)"

        if len(result) > self.OUTPUT_CHAR_CAP:
            whole = False
            result = result[:self.OUTPUT_CHAR_CAP] + f"\n\n... (output truncated at {self.OUTPUT_CHAR_CAP:,} chars. Use offset/limit to read specific sections.)"

        return result, whole

    async def execute(self, path: str, offset: int | None = None, limit: int | None = None, **kwargs: Any) -> str:
        try:
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={"path": path, "offset": offset, "limit": limit},
                path=path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=kwargs.get("_thread_id"),
                project_id=kwargs.get("_thread_project_id"),
            )
            outcome = await asyncio.to_thread(
                self._read_authorized_file,
                authorized.resolved,
                offset,
                limit,
            )
        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"
        # The observer touches only loop-owned document and read-base state.
        record_read_base(kwargs.get("_thread_id"), authorized.resolved, outcome.data)
        await self._record_skill_read(authorized.resolved, outcome, kwargs)
        from tools.workflow_task_fields import skill_read_notice

        annotation = await self._shared_document_annotation(
            authorized.resolved, outcome, offset,
        ) or await skill_read_notice(authorized.resolved, outcome.data, offset)
        annotation = sanitize_credential_text(annotation) if annotation else None
        if not annotation:
            return outcome.text
        rendered = f"{annotation}\n{outcome.text}"
        return CredentialSafeText(rendered) if isinstance(outcome.text, CredentialSafeText) else rendered

    @staticmethod
    async def _shared_document_annotation(
        resolved: Path,
        outcome: _ReadOutcome,
        offset: int | None,
    ) -> str | None:
        """Return an unnumbered registry annotation for a first-page shared read."""
        if outcome.data is None or offset not in (None, 1):
            return None
        if "shared" not in {part.casefold() for part in resolved.parts}:
            return None

        def _annotation() -> str | None:
            from infra import config
            from infra.database import get_db
            from registry import service
            from registry.doc_freshness import (
                artifact_freshness, format_freshness, verification_hint,
            )
            from registry.shared_principals import is_shared_document_path

            db = get_db()
            try:
                if not is_shared_document_path(
                    resolved, workspace=config.WORKSPACE, db=db
                ):
                    return None
                artifact = service.resolve_file_identity(str(resolved), db=db)
                projection = artifact_freshness(artifact, resolved, db=db)
                if projection is None:
                    return None
                owner_name = str(projection["owner_name"])
                banner = f"[Shared document · {format_freshness(projection, owner_name)}]"
                hint = verification_hint(projection, str(artifact["id"]))
                return f"{banner} {hint}" if hint else banner
            finally:
                db.close()

        return await run_in_db(_annotation)

    @staticmethod
    async def _record_skill_read(
        resolved: Path, outcome: _ReadOutcome, kwargs: dict[str, Any],
    ) -> None:
        """Leave a version receipt when a Turn reads a skill it was not given."""
        from core.skill_validation import is_skill_file

        if outcome.data is None or not is_skill_file(resolved):
            return
        from core.guidance_manifest import record_guidance_load
        from infra import state as runtime_state

        identity = runtime_state._current_turn_identity.get(None)
        await asyncio.to_thread(
            record_guidance_load,
            thread_id=kwargs.get("_thread_id"),
            turn_id=getattr(identity, "turn_id", None),
            path=resolved,
            data=outcome.data,
            returned_text=outcome.text,
            partial=not outcome.whole,
        )


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates parent directories if needed. Overwrites existing content.\n\n"
            "USE THIS when you need to:\n"
            "- Create a new file\n"
            "- Completely replace a file's contents\n\n"
            "DO NOT use this when:\n"
            "- Making targeted changes to an existing file (use edit_file instead — it's safer)\n\n"
            "IMPORTANT: Prefer edit_file for modifying existing files. Only use write_file "
            "for new files or complete rewrites.\n\n"
            "When a document is open in the live editor, read_file first. A whole write merges "
            "your changes with live edits. If the result says merged, read again before another write. "
            "A stale-base refusal writes nothing: read again, then retry. "
            "Written but not saved on the server means the file was written; do not repeat that write."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write to (absolute or relative to workspace)"
                },
                "content": {
                    "type": "string",
                    "description": "The full content to write to the file"
                },
                "description": {
                    "type": "string",
                    "description": "One-line summary for the artifact registry. Required when creating a new shared file; optional but encouraged otherwise."
                },
                "owner": {
                    "type": "string",
                    "description": "For a new shared file with no persistent caller: an agent name or ID, conductor, or user."
                },
                "update_trigger": {
                    "type": "string",
                    "description": "Required when creating a new shared file: when its owner must revisit it. Dated paths infer this automatically."
                },
                **GUIDANCE_PARAMETERS,
            },
            "required": ["path", "content"]
        }

    async def execute(
        self,
        path: str,
        content: str,
        description: str | None = None,
        owner: str | None = None,
        update_trigger: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={
                    "path": path,
                    "content": content,
                    "description": description,
                    "owner": owner,
                    "update_trigger": update_trigger,
                },
                path=path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=kwargs.get("_thread_id"),
                project_id=kwargs.get("_thread_project_id"),
                for_write=True,
            )
            file_path = authorized.resolved
            write_args = {
                "reviewed": authorized.reviewed, "producer": self.name,
                "description": description, "owner": owner,
                "update_trigger": update_trigger,
                "success": write_success_result(file_path, len(content)),
            }
            target = await asyncio.to_thread(guidance_target, file_path)
            if target is not None:
                return await save_guided_write_and_announce(file_path, content, target, kwargs, **write_args)
            result = await write_live(file_path, kwargs, producer=self.name, content=content, description=description)
            if result is not None:
                return result
            return announce_published_write(
                await run_off_db(managed_write, file_path, content, kwargs, notify=False, **write_args)
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"
