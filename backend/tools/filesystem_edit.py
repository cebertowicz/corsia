"""Edit and list directory tools.

Contains EditFileTool for targeted string replacement in files and
ListDirTool for listing directory contents.
"""

import asyncio
from pathlib import Path
from typing import Any

from eval.tool_allowlist import carry_in_hides
from infra.db_async import run_off_db
from infra.guidance_targets import guidance_target
from infra.guidance_history_paths import bytes_before_text
from tools.base import Tool
from tools.file_write_completion import (
    GUIDANCE_PARAMETERS,
    announce_published_write,
    managed_write,
    save_guided_write_and_announce,
)
from tools.filesystem_effects import list_bound_directory, open_bound_text
from tools.filesystem_gate import _resolve_path_for_action
from tools.filesystem_match import match_edits
from doceditor.live_lane import TextEdit
from doceditor.live_tools import write_live


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
                fence=kwargs.get("_host_access_fence"),
                for_write=True,
            )
            file_path = authorized.resolved
            target = await asyncio.to_thread(guidance_target, file_path)
            if target is None:
                result = await write_live(file_path, kwargs, producer=self.name,
                                          edits=(TextEdit(old_string, new_string, replace_all),), edit_path=path)
                if result is not None:
                    return result
            # The save compares the file's bytes, so the expectation is read before the text read does.
            expected = bytes_before_text(file_path) if target is not None else None
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
                file_path, new_content, target, kwargs, expected=expected, **write_args,
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
                fence=kwargs.get("_host_access_fence"),
                for_write=True,
            )
            file_path = authorized.resolved
            target = await asyncio.to_thread(guidance_target, file_path)
            if target is None:
                captured = tuple(TextEdit(edit["old_string"], edit["new_string"], bool(edit.get("replace_all"))) for edit in edits)
                result = await write_live(file_path, kwargs, producer=self.name, edits=captured, edit_path=path)
                if result is not None:
                    return result
            expected = bytes_before_text(file_path) if target is not None else None
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
                file_path, content, target, kwargs, expected=expected, **write_args,
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
                fence=kwargs.get("_host_access_fence"),
            )
            entries = await asyncio.to_thread(list_bound_directory, authorized.resolved)
            items = []
            for name, is_dir in sorted(entries):
                if carry_in_hides(authorized.resolved / name):
                    continue
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
