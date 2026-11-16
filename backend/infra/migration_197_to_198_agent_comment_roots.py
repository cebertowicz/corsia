"""Schema 197 -> 198: store the comments an agent leaves on its own chat messages.

Adds ``chat_comment_agent_roots`` and its index (``chat_comment_schema``). Additive: no agent
root exists before this step, so there is nothing to backfill. The step runs inside the
runner's transaction; it never commits or rolls back.
"""

from infra.chat_comment_schema import AGENT_ROOT_SCHEMA_SQL


def migrate_197_to_198(db):
    """Create the agent-root table and index; every statement is IF NOT EXISTS."""
    for statement in AGENT_ROOT_SCHEMA_SQL.split(";"):
        if statement.strip():
            db.execute(statement)
