"""Project and thread query tools.

Contains GetThreadHistoryTool, ListProjectsTool, and SetConversationProjectTool
for retrieving thread messages and managing project associations.
"""

from typing import Any

from core.message_display import durable_row_text
from core.registered_repos import format_registered_repo_policy
from infra.db_async import run_in_db
from infra.passive_changes import append_agent_roster_transition
from tools.base import Tool
from tools.query_helpers import (
    format_projects_list,
    list_projects_query,
)


#: What a tool result is trimmed to under the default 'preview' mode. Enough to
#: recognise which call it was and whether its output is the one you need,
#: without paying for the whole thing — re-reading history is the step that
#: silently refills the context a checkpoint was run to shrink.
_TOOL_PREVIEW_CHARS = 240
_MESSAGE_PREVIEW_CHARS = 500


def _history_line(row: Any, tool_output: str) -> str:
    """Render one history row, led by the number a checkpoint would cite."""
    content = durable_row_text(row["content"], row["metadata"])
    is_tool = row["role"] == "tool"
    if tool_output == "full":
        body = content
    else:
        limit = _TOOL_PREVIEW_CHARS if is_tool else _MESSAGE_PREVIEW_CHARS
        if len(content) > limit:
            noun = "result" if is_tool else "message"
            body = (
                content[:limit]
                + f"… [trimmed, {len(content)} chars total — for this one {noun} "
                f"call again with from_seq={row['seq']}, to_seq={row['seq']}, "
                "tool_output='full']"
            )
        else:
            body = content
    return f"#{row['seq']} [{str(row['created_at'])[:16]}] {row['role'].upper()}: {body}"


class GetThreadHistoryTool(Tool):
    """Get message history from a specific thread."""

    @property
    def name(self) -> str:
        return "get_thread_history"

    @property
    def description(self) -> str:
        return (
            "Get the full or filtered message history from a specific thread.\n\n"
            "USE THIS when you need to:\n"
            "- Review what happened in a specific thread\n"
            "- Get context before continuing work started in another thread\n"
            "- Check the exact messages exchanged (not just a summary)\n\n"
            "Can filter by role and limit the number of messages."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "The thread ID to get history from"
                },
                "role": {
                    "type": "string",
                    "enum": ["user", "assistant", "tool", "system"],
                    "description": "Filter by message role (optional — all roles if omitted)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum messages to return (default 50, most recent first)",
                    "minimum": 1,
                    "maximum": 200
                },
                "oldest_first": {
                    "type": "boolean",
                    "description": "Return oldest messages first (default false — newest first)"
                },
                "from_seq": {
                    "type": "integer",
                    "description": (
                        "Only messages at or after this message number. Every "
                        "result line begins with its number, and checkpoints "
                        "cite these numbers — so this is how you re-read the "
                        "exact range a checkpoint referred to."
                    ),
                    "minimum": 1
                },
                "to_seq": {
                    "type": "integer",
                    "description": "Only messages at or before this message number.",
                    "minimum": 1
                },
                "tool_output": {
                    "type": "string",
                    "enum": ["preview", "full", "omit"],
                    "description": (
                        "How much of each tool result or message to include. "
                        "'preview' (default) shows the opening of each so you "
                        "can judge whether you need it; 'omit' drops tool results "
                        "entirely when you only want the messages; 'full' returns "
                        "whole tool results and whole messages. Re-reading history "
                        "with 'full' puts everything "
                        "you already folded back into your context, so ask for "
                        "one exact row instead: from_seq=N, to_seq=N, "
                        "tool_output='full'."
                    )
                },
            },
            "required": ["thread_id"]
        }

    async def execute(
        self,
        thread_id: str,
        role: str | None = None,
        limit: int = 50,
        oldest_first: bool = False,
        from_seq: int | None = None,
        to_seq: int | None = None,
        tool_output: str = "preview",
        **kwargs: Any,
    ) -> str:
        from infra.database import get_db

        def _query():
            db = get_db()
            thread_row = db.execute("SELECT title FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not thread_row:
                db.close()
                return None, None
            where = ["thread_id = ?", "delivery_state = 'delivered'", "seq IS NOT NULL"]
            params: list[Any] = [thread_id]
            if role:
                where.append("role = ?")
                params.append(role)
            if from_seq is not None:
                where.append("seq >= ?")
                params.append(from_seq)
            if to_seq is not None:
                where.append("seq <= ?")
                params.append(to_seq)
            if tool_output == "omit":
                where.append("role <> 'tool'")
            order = "ASC" if oldest_first else "DESC"
            rows = db.execute(
                f"SELECT seq, role, content, metadata, created_at FROM messages "
                f"WHERE {' AND '.join(where)} ORDER BY seq {order} LIMIT ?",
                (*params, limit),
            ).fetchall()
            db.close()
            return thread_row, rows

        thread_row, rows = await run_in_db(_query)

        if thread_row is None:
            return f"Error: Thread '{thread_id}' not found"
        if not rows:
            return f"No messages found in thread '{thread_id}'"
        if not oldest_first:
            rows = list(reversed(rows))

        title = thread_row["title"] or "Untitled"
        lines = [f"Thread \"{title}\" ({thread_id}) — {len(rows)} messages:\n"]
        lines.extend(_history_line(row, tool_output) for row in rows)
        return "\n".join(lines)


class ListProjectsTool(Tool):
    """List registered projects."""

    @property
    def name(self) -> str:
        return "project_list"

    @property
    def description(self) -> str:
        return (
            "List registered projects with their status, workspace reference, "
            "and canonical conductor_thread_id.\n\n"
            "USE THIS when you need to:\n"
            "- See what projects exist\n"
            "- Find the workspace reference for a project\n"
            "- Check project status or resume a handoff to its conductor\n\n"
            "Use a returned conductor_thread_id with delegate_send and "
            "delegate_status. If no conductor is designated, the listing says "
            "so; do not guess an address. A designation does not prove the "
            "conductor has a model or has completed work."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "archived", "all"],
                    "description": "Filter by status (default 'active')"
                },
            },
        }

    async def execute(self, status: str = "active", **kwargs: Any) -> str:
        from infra.database import get_db

        def _query():
            db = get_db()
            rows = list_projects_query(db, status)
            db.close()
            return rows

        rows = await run_in_db(_query)
        return format_projects_list(rows, status)


class ProjectReposTool(Tool):
    """List the repositories registered to the current project."""

    @property
    def name(self) -> str:
        return "project_repos"

    @property
    def description(self) -> str:
        return (
            "List the repositories registered to the current project, with each "
            "repository's path, pinned branch, and base-checkout protection "
            "status.\n\n"
            "USE THIS when you need to:\n"
            "- See which repositories a project has before creating a worktree\n"
            "- Find the exact repo_name to pass to worktree_create\n"
            "- Check a repository's pin/protection policy\n\n"
            "Requires the conversation to be attached to a project."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> str:
        from infra.project_repo_policy import get_project_repo_policies

        project_id = kwargs.get("_thread_project_id")
        if not isinstance(project_id, str) or not project_id:
            return "Error: project_repos requires a current project"

        policies = await run_in_db(lambda: get_project_repo_policies(project_id))
        if not policies:
            return (
                "No repositories are registered to this project. Register a "
                "source repository with project_repo_register (or in Project "
                "settings) to enable worktree_create."
            )

        lines = []
        for name in sorted(policies):
            lines.append(f"- {format_registered_repo_policy(name, policies[name])}")
        return "\n".join(lines)


class SetConversationProjectTool(Tool):
    """Attach or detach a conversation to/from a project."""

    @property
    def name(self) -> str:
        return "conversation_set_project"

    @property
    def description(self) -> str:
        return (
            "Attach a conversation to a project, or detach it. "
            "When a conversation is attached to a project, the agent gets project-scoped context "
            "(PROJECT.md, BRIEF.md, working files) loaded into every turn.\n\n"
            "USE THIS when:\n"
            "- Starting work on a project in a conversation\n"
            "- The user says 'this is for project X'\n"
            "- You need project context loaded automatically\n\n"
            "To detach, pass project_id as null."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "The conversation thread ID to update. Use the current Chat ID from runtime context.",
                },
                "project_id": {
                    "type": ["string", "null"],
                    "description": "Project ID to attach to, or null to detach.",
                },
            },
            "required": ["conversation_id"],
        }

    async def execute(
        self, conversation_id: str, project_id: str | None = None, **kwargs: Any
    ) -> str:
        from infra.database import write_transaction
        from infra.time import utc_now_iso

        def _query():
            with write_transaction(
                None, scope="conversation_set_project"
            ) as db:
                conv = db.execute(
                    "SELECT * FROM threads WHERE id = ?", (conversation_id,)
                ).fetchone()
                if not conv:
                    return "not_found", None, None, False

                project_name = None
                if project_id:
                    proj = db.execute(
                        "SELECT id, name FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                    if not proj:
                        return "project_not_found", None, None, False
                    project_name = proj["name"]

                now = utc_now_iso()
                changed = project_id != conv["project_id"]
                db.execute(
                    "UPDATE threads SET project_id = ?, updated_at = ? WHERE id = ?",
                    (project_id, now, conversation_id),
                )
                from connections.account_holds import refresh_thread_holds

                refresh_thread_holds(conversation_id, db=db)
                if changed:
                    from plans.public import rehome

                    rehome(conversation_id, db=db)
                    updated = dict(conv)
                    updated["project_id"] = project_id
                    append_agent_roster_transition(
                        db,
                        before=conv,
                        after=updated,
                        summary=f"Agent responsibility map updated: {conv['title'] or 'Untitled'}",
                    )
                return "ok", dict(conv), project_name, changed

        status, conv, project_name, changed = await run_in_db(_query)

        if status == "not_found":
            return f"Conversation '{conversation_id}' not found"
        if status == "project_not_found":
            return f"Project '{project_id}' not found"
        if changed:
            from thread.runtime_boundary import post_context_boundary_effects

            await post_context_boundary_effects(
                conversation_id,
                user_id=None,
                reason="session_context_changed",
            )

        if project_id:
            return (
                f"Conversation '{conv['title']}' attached to project '{project_name}'. "
                "Project context will load on next turn."
            )
        old_project = conv["project_id"]
        return f"Conversation '{conv['title']}' detached from project. Was: {old_project}"
