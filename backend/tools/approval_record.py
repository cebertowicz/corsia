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
        from tools.delegation import _require_context
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor = context

        def read_scoped():
            from infra.database import get_db
            from plans.public import approved_versions_for_pr
            from tools.document_comment_target import resolve_doc_target
            from tools.filesystem_gate import resolve_path

            db = get_db()
            try:
                result = approved_versions_for_pr(*(kwargs[name] for name in names), db=db)
                visible = []
                for artifact in result['artifacts']:
                    try:
                        resolve_doc_target(doc_path=None, artifact_id=artifact['artifact_id'],
                                           actor_thread_id=thread_id, user_id=actor['user_id'],
                                           db=db, allow_register=False)
                        resolve_path(artifact['path'], thread_id=thread_id)
                    except (PermissionError, ValueError):
                        continue
                    visible.append(artifact)
                result['artifacts'] = visible
                return result
            finally:
                db.close()

        return json.dumps(await run_in_db(read_scoped))
