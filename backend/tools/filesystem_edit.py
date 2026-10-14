"""Edit and list directory tools.

Contains EditFileTool for targeted string replacement in files and
ListDirTool for listing directory contents.
"""

import asyncio
from collections import Counter
import difflib
from pathlib import Path
from typing import Any

from infra.db_async import run_off_db
from tools.base import Tool
from tools.file_write_completion import (
    GUIDANCE_PARAMETERS,
    announce_published_write,
    guidance_target,
    managed_write,
    save_guided_write_and_announce,
)
from tools.filesystem_effects import list_bound_directory, open_bound_text
from tools.filesystem_gate import _resolve_path_for_action
from doceditor.live_lane import TextEdit
from doceditor.live_tools import write_live


_MATCH_CANDIDATES = 5


def _candidate_starts(lines: list[str], old_lines: list[str], last_start: int) -> list[int]:
    """Window starts worth a character-level comparison, best first.

    A character ratio is quadratic in the window's length, so it runs only on
    the few windows that share the most lines with old_string (ignoring
    indentation). When no line is shared, a close line to the first non-blank
    line of old_string anchors the window instead; lines are short, so that
    comparison stays cheap.
    """
    positions: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        if line.strip():
            positions.setdefault(line.strip(), []).append(index)
    votes: Counter[int] = Counter()
    for offset, line in enumerate(old_lines):
        for index in positions.get(line.strip(), ()):
            if 0 <= index - offset <= last_start:
                votes[index - offset] += 1
    if votes:
        return [start for start, _count in votes.most_common(_MATCH_CANDIDATES)]
    anchor = next(((offset, line) for offset, line in enumerate(old_lines) if line.strip()), None)
    if anchor is None:
        return [0]
    offset, text = anchor
    scored = []
    for index, line in enumerate(lines):
        if 0 <= index - offset <= last_start:
            matcher = difflib.SequenceMatcher(None, text, line)
            if matcher.real_quick_ratio() > 0.5 and matcher.quick_ratio() > 0.5:
                scored.append((matcher.ratio(), index - offset))
    scored.sort(reverse=True)
    return [start for _ratio, start in scored[:_MATCH_CANDIDATES]] or [0]


def _not_found_message(old_string: str, content: str, path: str, prefix: str = "") -> str:
    """Build a helpful error when old_string is not found."""
    lines = content.splitlines(keepends=True)
    old_lines = old_string.splitlines(keepends=True)
    window = len(old_lines)

    best_ratio, best_start = 0.0, 0
    for i in _candidate_starts(lines, old_lines, max(0, len(lines) - window)):
        ratio = difflib.SequenceMatcher(None, old_string, "".join(lines[i : i + window])).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i

    if best_ratio > 0.5:
        diff = "\n".join(difflib.unified_diff(
            old_lines, lines[best_start : best_start + window],
            fromfile="old_string (provided)", tofile=f"{path} (actual, line {best_start + 1})",
            lineterm="",
        ))
        return f"Error: {prefix}old_string not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
    return f"Error: {prefix}old_string not found in {path}. No similar text found. Verify the file content."


def match_edits(original: str, edits: tuple[TextEdit, ...], path: str, *, multi: bool,
                error_path: str | None = None) -> tuple[str | None, str]:
    """The live lane and closed-file tools share matching and model receipts."""
    content, applied = original, 0
    for idx, edit in enumerate(edits, start=1):
        if edit.old not in content:
            message = _not_found_message(edit.old, content, error_path or path, prefix=f"edit {idx} " if multi else "")
            if multi:
                message += "\nNo changes were written. Re-read the file, then retry."
            return None, message
        count = content.count(edit.old)
        if count > 1 and not edit.replace_all:
            message = (
                f"Error: edit {idx} old_string appears {count} times. "
                "Provide more context or set replace_all=true. No changes were written."
            ) if multi else (
                f"Warning: old_string appears {count} times. Provide more context to make it unique, or use replace_all=true."
            )
            return None, message
        content = content.replace(edit.old, edit.new, -1 if edit.replace_all else 1)
        applied += count if edit.replace_all else 1
    if multi:
        message = f"No changes needed in {path}" if content == original else (
            f"Successfully applied {len(edits)} edit(s), {applied} replacement(s), to {path}"
        )
    else:
        message = f"Successfully replaced {applied} occurrence(s) in {path}" if edits and edits[0].replace_all else f"Successfully edited {path}"
    return content, message


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing an exact string match with new text.\n\n"
            "By default, old_string must appear exactly once in the file. If it appears "
            "multiple times, either provide more surrounding context to make it unique, "
            "or set replace_all=true to replace every occurrence (useful for renames).\n\n"
            "USE THIS when you need to:\n"
            "- Make targeted changes to existing files\n"
            "- Fix bugs, update config values, modify specific functions\n"
            "- Rename a variable or string across a file (with replace_all=true)\n"
            "- Any change where you're modifying part of a file (not rewriting the whole thing)\n\n"
            "IMPORTANT:\n"
            "- Read the file first, and again right before editing if it may have changed\n"
            "- Include enough context in old_string to make the match unique\n"
            "- Preserve exact indentation (tabs/spaces) from the original"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to edit (absolute or relative to workspace)"
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace"
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text (must be different from old_string)"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace ALL occurrences of old_string (default false). Useful for renaming variables."
                },
                **GUIDANCE_PARAMETERS,
            },
            "required": ["path", "old_string", "new_string"]
        }

    async def execute(self, path: str, old_string: str, new_string: str, replace_all: bool = False, **kwargs: Any) -> str:
        # Ordinary writes register from the persisted result observer; governed
        # shared writes register atomically inside the durable-write transaction.
        try:
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={
                    "path": path,
                    "old_string": old_string,
                    "new_string": new_string,
                    "replace_all": replace_all,
                },
                path=path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=kwargs.get("_thread_id"),
                project_id=kwargs.get("_thread_project_id"),
                for_write=True,
            )
            file_path = authorized.resolved
            target = await asyncio.to_thread(guidance_target, file_path)
            if target is None:
                result = await write_live(file_path, kwargs, producer=self.name,
                                          edits=(TextEdit(old_string, new_string, replace_all),), edit_path=path)
                if result is not None:
                    return result
            with open_bound_text(file_path, writable=target is None) as (handle, _file_stat):
                content = handle.read()

                new_content, success = match_edits(content, (TextEdit(old_string, new_string, replace_all),), str(file_path), multi=False, error_path=path)
                if new_content is None:
                    return success
                write_args = {"reviewed": authorized.reviewed, "producer": self.name, "success": success}
                if target is None:
                    return announce_published_write(
                        await run_off_db(
                            managed_write, file_path, new_content, kwargs,
                            notify=False, handle=handle, **write_args,
                        )
                    )
            return await save_guided_write_and_announce(
                file_path, new_content, target, kwargs, expected=content, **write_args,
            )
        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class MultiEditTool(Tool):
    """Apply multiple exact string replacements to one file atomically."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "multi_edit"

    @property
    def description(self) -> str:
        return (
            "Apply multiple ordered exact-string edits to one file. The file is "
            "written only if every edit can be applied, so a missed match leaves "
            "the file unchanged. Use this for several related edits in the same "
            "file; use edit_file for one edit. On a miss, re-read the file."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to edit (absolute or relative to workspace)",
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": "Exact text to find and replace",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "Replacement text",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace all occurrences for this edit",
                            },
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
                **GUIDANCE_PARAMETERS,
            },
            "required": ["path", "edits"],
        }

    async def execute(self, path: str, edits: list[dict[str, Any]], **kwargs: Any) -> str:
        try:
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={"path": path, "edits": edits},
                path=path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=kwargs.get("_thread_id"),
                project_id=kwargs.get("_thread_project_id"),
                for_write=True,
            )
            file_path = authorized.resolved
            target = await asyncio.to_thread(guidance_target, file_path)
            if target is None:
                captured = tuple(TextEdit(edit["old_string"], edit["new_string"], bool(edit.get("replace_all"))) for edit in edits)
                result = await write_live(file_path, kwargs, producer=self.name, edits=captured, edit_path=path)
                if result is not None:
                    return result
            with open_bound_text(file_path, writable=target is None) as (handle, _file_stat):
                original = handle.read()
                captured = tuple(TextEdit(edit["old_string"], edit["new_string"], bool(edit.get("replace_all"))) for edit in edits)
                content, success = match_edits(original, captured, str(file_path), multi=True, error_path=path)
                if content is None or content == original:
                    return success
                write_args = {"reviewed": authorized.reviewed, "producer": self.name, "success": success}
                if target is None:
                    return announce_published_write(
                        await run_off_db(
                            managed_write, file_path, content, kwargs,
                            notify=False, handle=handle, **write_args,
                        )
                    )
            return await save_guided_write_and_announce(
                file_path, content, target, kwargs, expected=original, **write_args,
            )
        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """Tool to list directory contents."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory, showing files and subdirectories.\n\n"
            "USE THIS when you need to:\n"
            "- See what's in a specific directory\n"
            "- Verify a directory exists before working in it\n"
            "- Get an overview of a project's top-level structure\n\n"
            "DO NOT use this when:\n"
            "- Searching for files across many directories (use glob instead)\n"
            "- Looking for specific file content (use grep instead)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (absolute or relative to workspace)"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            authorized = await _resolve_path_for_action(
                tool=self,
                tool_name=self.name,
                params={"path": path},
                path=path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
                thread_id=kwargs.get("_thread_id"),
                project_id=kwargs.get("_thread_project_id"),
            )
            entries = await asyncio.to_thread(list_bound_directory, authorized.resolved)
            items = []
            for name, is_dir in sorted(entries):
                prefix = "d " if is_dir else "f "
                items.append(f"{prefix}{name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except FileNotFoundError:
            return f"Error: Directory not found: {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"
