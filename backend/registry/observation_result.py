"""Result and event-loop delivery for the blocking registry observer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from doceditor.presence import DocumentActivity, publish_document_activity
from registry.registration_writer import RegistrationOrigin


@dataclass(frozen=True)
class ObservationResult:
    """Independent artifact and ephemeral activity outcomes from one result."""

    artifact: dict[str, Any] | None = None
    activity: DocumentActivity | None = None


def observation_result(
    artifact: dict[str, Any] | None = None,
    activity: DocumentActivity | None = None,
) -> ObservationResult | None:
    return ObservationResult(artifact, activity) if artifact or activity else None


async def observe_settled_tool_result(
    thread_id: str,
    content: str,
    metadata: dict,
    *,
    origin: RegistrationOrigin | None = None,
    db=None,
) -> None:
    """Observe one committed tool result and notify from the caller's event loop."""
    from infra.db_async import run_in_db
    from registry.observer import observe_record
    from registry.service import notify_changed

    try:
        observation = await run_in_db(
            lambda: observe_record(
                thread_id, "tool", content, metadata, db=db, origin=origin
            )
        )
        if observation and observation.artifact:
            notify_changed(observation.artifact)
        if observation and observation.activity:
            publish_document_activity(observation.activity)
    except Exception as error:
        from loguru import logger

        logger.warning(
            f"registration observer failed for session {thread_id}: {error}"
        )
