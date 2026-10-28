"""Observe an in-memory stamped connected-app result."""
from __future__ import annotations

from infra.database import get_db
from infra.tool_result_errors import is_error_result
from registry.observer import observe_command_result
from registry.registration_writer import RegistrationOrigin
from registry.service import notify_changed
from tools.mcp_stamp import result_stamp


def observe_current_result(result: object, args: dict) -> object:
    """Observe the clean result before any tool notices are appended."""
    from events.self_caused import record_result
    from infra import state
    from loguru import logger
    from tools.mcp_stamp import restamp

    thread_id = state._current_thread_id.get(None)
    turn = state._current_turn_identity.get(None)
    notices = []
    try:
        record_result(thread_id, result)
    except Exception:
        logger.exception("Could not record connected-app event identity")
        notices.append("Couldn't record this result's event identity. Your own event may wake you again.")
    if thread_id and turn is not None:
        try:
            notices.extend(notice_for_result(thread_id, turn.turn_id, args, result))
        except Exception:
            logger.exception("connected-app observation failed")
            notices.append("Couldn't check whether to register this result. Register it manually if needed.")
    return restamp(result, str(result) + "\n\n" + "\n".join(notices)) if notices else result


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
