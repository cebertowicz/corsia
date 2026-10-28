"""Observe an in-memory stamped connected-app result."""
from __future__ import annotations

from infra.database import get_db
from infra.tool_result_errors import is_error_result
from registry.observer import observe_command_result
from registry.registration_writer import RegistrationOrigin
from registry.service import notify_changed
from tools.mcp_stamp import result_stamp


def notice_for_result(thread_id: str, turn_id: str, args: dict, result: object) -> list[str]:
    """Keep an observer failure from withholding a completed tool result."""
    db = get_db()
    changed: list[dict] = []
    told: list[str] = []
    try:
        observe_command_result(thread_id, None, args, str(result), metadata=result_stamp(result),
                               db=db, told=told, changed=changed, is_error=is_error_result(result),
                               origin=RegistrationOrigin(thread_id, turn_id, "observe_rule"))
    except Exception:
        from loguru import logger

        logger.exception("connected-app observation failed")
        return ["Couldn't check whether to register this result. Register it manually if needed."]
    finally:
        db.close()
    for artifact in changed:
        notify_changed(artifact)
    return told
