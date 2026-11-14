"""Promote one thread-owned working file into approved durable shared scope."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doceditor.live_move_hold import hold_for_move
from doceditor.live_runtime import get_documents
from infra.database import get_db
from infra.db_async import run_in_db
from registry import service
from registry.promotion_source import promotion_source
from registry.registration_writer import origin_from_tool_context
from registry.durable_writes import promote_file
from tools.base import Tool
from tools.delegation import _require_context


class PromoteArtifactTool(Tool):
    """Move a file from this thread's working tray into shared/<category>/."""

    def __init__(self, workspace: Path | None = None):
        from infra.config import WORKSPACE

        self._workspace = workspace or WORKSPACE

    @property
    def name(self) -> str:
        return "promote_artifact"

    @property
    def description(self) -> str:
        return (
            "Move a durable output from your exact .threads/<id>/working/ tray "
            "into an existing approved shared/<category>/ directory and register "
            "it atomically. The source is removed only when the shared file and "
            "artifact identity both succeed. Exact destination collisions fail."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File under this thread's exact working tray "
                        "(absolute or workspace-relative)."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": "Existing approved shared category name.",
                },
                "description": {
                    "type": "string",
                    "description": "What this durable artifact is and why it matters.",
                },
                "owner": {
                    "type": "string",
                    "description": "When there is no persistent caller: an agent name or ID, conductor, or user.",
                },
                "update_trigger": {
                    "type": "string",
                    "description": "Required for a new shared artifact: when the owner must revisit it; dated paths infer it automatically.",
                },
            },
            "required": ["path", "category", "description"],
        }

    async def execute(
        self,
        path: str,
        category: str,
        description: str,
        owner: str | None = None,
        update_trigger: str | None = None,
        **kwargs: Any,
    ) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor = context
        # The constructor workspace is only the base directory for relative
        # source paths (a harness session's working dir may be a project
        # directory). Durable-write governance always resolves scope against
        # the canonical selected workspace root (#830).
        from infra.config import WORKSPACE

        source = Path(path).expanduser()
        if not source.is_absolute():
            source = self._workspace / source
        async def promote() -> dict:
            return await run_in_db(
                promote_file,
                source,
                category,
                thread_id=thread_id,
                project_id=actor.get("project_id"),
                workspace=WORKSPACE,
                description=description,
                owner=owner,
                update_trigger=update_trigger,
                origin=origin_from_tool_context(
                    thread_id, kwargs, "promote_artifact"
                ),
                notify=False,
            )

        def check() -> Path:
            db = get_db()
            try:
                return promotion_source(
                    source, category, thread_id=thread_id, project_id=actor.get("project_id"),
                    workspace=WORKSPACE, db=db,
                )[1]
            finally:
                db.close()

        try:
            # The checks that can refuse a promote run first and hold nothing, so a refused call closes
            # nobody's document; promote_file runs them again under the hold.
            source_path = await run_in_db(check)
            documents = get_documents()
            if documents is None:
                receipt = await promote()
            else:
                # A person may have this file open: their typing is saved into it before it moves.
                async with hold_for_move(documents, source_path):
                    receipt = await promote()
        except Exception as error:
            return f"Error: {error}"
        service.notify_changed(receipt["artifact"])
        return json.dumps(receipt, sort_keys=True)
