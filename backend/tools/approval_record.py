"""Read approved document versions tied to a pull request."""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool


class ApprovalRecordTool(Tool):
    """Expose the approved-version read to a rule-run script."""

    @property
    def name(self) -> str:
        return "approval_record"

    @property
    def description(self) -> str:
        return ("Read the approved document versions tied to a pull request "
                "(Quality Gate's stage script). Pass integration, type, and external_id.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "integration": {"type": "string"},
                "type": {"type": "string"},
                "external_id": {"type": "string"},
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        if kwargs.get("mode") != "read":
            return 'Only mode "read" is available.'
        names = ("integration", "type", "external_id")
        if any(not isinstance(kwargs.get(name), str) for name in names):
            return "integration, type, and external_id are required strings."
        from plans.public import approved_versions_for_pr

        result = await run_in_db(approved_versions_for_pr, *(kwargs[name] for name in names))
        return json.dumps(result)
