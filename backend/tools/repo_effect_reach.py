"""What one admitted tool call could write, for the repository policy fence.

``ToolRegistry`` admits every call that can write as a repository effect carrying
this reach (``infra.repo_policy_transition``); a file tool that only reads is not
admitted at all.
"""

from __future__ import annotations

import logging
from typing import Any

from infra.repo_policy_transition import EVERYWHERE, Reach, caller_reach

log = logging.getLogger(__name__)


async def effect_reach(
    tool: Any, name: str, params: dict[str, Any], thread_id: str, authorization: Any,
) -> Reach:
    """What this call's effect could write, read once at admission.

    A file-writing tool writes only its target path, under a permit or not. A call
    under a host-action permit, or to a connected app (whose local server is walled
    only from Local only projects), reaches everywhere. Otherwise the caller's
    reach; a caller that cannot be read reaches everywhere.
    """
    from infra.database import get_db
    from infra.db_async import run_in_db
    from tools.host_access_gate import written_path
    from tools.mcp import MCPToolWrapper

    if (target := written_path(name, params, tool=tool)) is not None:
        return Reach(paths=tuple(dict.fromkeys(target)))
    if getattr(authorization, "_permit", None) is not None or isinstance(tool, MCPToolWrapper):
        return EVERYWHERE

    def _read() -> Reach:
        db = get_db()
        try:
            return caller_reach(thread_id, db=db)
        finally:
            db.close()

    try:
        return await run_in_db(_read)
    except Exception:
        log.exception("repo effect reach read failed for thread=%s", thread_id)
        return EVERYWHERE
