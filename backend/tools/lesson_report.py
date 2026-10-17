"""Read-only lesson report; the registry supplies the caller identity."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.lesson_report_access import report_for_identity
from tools.base import Tool


class LessonReportTool(Tool):
    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "lesson_report"

    @property
    def description(self) -> str:
        return (
            "Read current lesson versions, due-first review work and recorded evidence. Defaults to self and 12w; "
            "evidence covers completed weeks only, excluding the current week even for range=all. "
            "all/project/agent are existing same-user view filters. Lists 20 rows, preview 5; "
            "pass next_cursor with the same filters for more. state=watch|trial lists only those; their rows carry a one-line statement. Detail needs lesson_id and preferably version. "
            "Indexed offers and body reads prove exposure, not use or benefit; unknowns stay null with reasons. "
            "Unidentified body receipts preserve raw hashes/references without guessing a behavioral version. "
            "A changed snapshot requires starting again without cursor. No lifecycle writes or model call."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "enum": ["self", "all", "project", "agent"]},
                "project": {"type": "string", "description": "Project id for project/agent scope; all otherwise."},
                "agent": {"type": "string", "description": "Agent id for agent scope."},
                "range": {"type": "string", "enum": ["12w", "26w", "all"]},
                "lesson_id": {"type": "string"}, "version": {"type": "string"},
                "cursor": {"type": "string", "maxLength": 1024}, "preview": {"type": "boolean"},
                "evidence": {"type": "string", "enum": ["unidentified_reads"]},
                "state": {"type": "string", "enum": ["watch", "trial"]},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        thread = kwargs.pop("_thread_id", None)
        kwargs.pop("_thread_project_id", None)
        kwargs.pop("_host_access_turn_identity", None)

        try:
            result = await report_for_identity(workspace=self._workspace, caller_thread_id=thread, **kwargs)
        except (ValueError, PermissionError) as exc:
            if isinstance(exc, PermissionError):
                next_step = "Caller identity comes from the runtime; restore the caller's existing thread identity."
            elif "scope differs" in str(exc):
                next_step = "Retry without cursor using the intended scope and filters."
            elif "cursor" in str(exc) or "report changed" in str(exc):
                next_step = "Refresh without cursor using the same scope and filters."
            else:
                next_step = "Use the reported cause and tool schema to correct the request."
            return json.dumps({"error": str(exc), "next": next_step}, separators=(",", ":"))
        return json.dumps(result, separators=(",", ":"))
