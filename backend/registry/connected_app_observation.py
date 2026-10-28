"""Observe an in-memory stamped connected-app result."""
from __future__ import annotations

import logging

from infra.tool_result_errors import is_error_result
from registry.observer import CommandArtifactRegistrationError, observe_command_result
from registry.registration_writer import RegistrationOrigin
from registry.service import notify_changed
from tools.mcp_stamp import result_stamp
from tools.shell_output import registration_warning as build_registration_warning

log = logging.getLogger(__name__)


async def observe_in_db(result: object, args: dict) -> object:
    """Observe on the DB pool, which doesn't carry context variables, so read the turn first."""
    from infra import state
    from infra.db_async import run_in_db

    turn = state._current_turn_identity.get(None)
    return await run_in_db(observe_current_result, result, args,
                           state._current_thread_id.get(None), getattr(turn, "turn_id", None))


def observe_current_result(result: object, args: dict, thread_id: str | None, turn_id: str | None) -> object:
    """Observe the clean result before any tool notices are appended."""
    from events.self_caused import record_result
    from loguru import logger
    from tools.mcp_stamp import restamp

    notices = []
    try:
        record_result(thread_id, result)
    except Exception:
        logger.exception("Could not record connected-app event identity")
        notices.append("Couldn't record this result's event identity. Your own event may wake you again.")
    if thread_id:
        notices.extend(notice_for_result(thread_id, turn_id, args, result))
    return restamp(result, str(result) + "\n\n" + "\n".join(notices)) if notices else result


def observe_with_warning(thread_id: str, tool: str | None, args: dict, content: str,
                         **kwargs) -> tuple[list[str], str | None]:
    """Observe once, returning notices and a warning while notifying partial changes."""
    told: list[str] = []
    changed: list[dict] = []
    warning = None
    try:
        observe_command_result(thread_id, tool, args, content, told=told, changed=changed, **kwargs)
    except CommandArtifactRegistrationError as exc:
        log.exception("connector command artifact registration failed in %s", thread_id)
        warning = build_registration_warning(exc.artifact, exc.error)
    except Exception as exc:
        log.exception("connector command observation failed in %s", thread_id)
        warning = build_registration_warning(None, exc)
    finally:
        for artifact in changed:
            notify_changed(artifact)
    return told, warning


def notice_for_result(thread_id: str, turn_id: str | None, args: dict, result: object) -> list[str]:
    """Keep an observer failure from withholding a completed tool result.

    With no turn (a fork), register with no origin, as the shell path does.
    """
    origin = RegistrationOrigin(thread_id, turn_id, "observe_rule") if turn_id else None
    told, warning = observe_with_warning(
        thread_id, None, args, str(result), metadata=result_stamp(result),
        is_error=is_error_result(result), origin=origin,
    )
    return told + ([warning] if warning else [])
