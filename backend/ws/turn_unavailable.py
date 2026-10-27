"""Model-unavailable presentation for native Turns."""

from __future__ import annotations

from loguru import logger

from infra import state
from llm.resolution import ModelUnavailableError
from ws.turn_helpers import NativeTurnRequest, reject_future, resolve_future


async def report_model_unavailable(
    request: NativeTurnRequest, error: ModelUnavailableError,
) -> bool:
    """Present an unavailable target after its preadmitted failure settles."""
    try:
        if request.ingress == "event":
            raise error
        logger.warning("ws.turn: model unavailable for {}: {}", request.thread_id[:8], error)
        await state.send_to_client(
            request.ws,
            {"type": "error", "thread_id": request.thread_id,
             "message": f"Model unavailable: {error.reason}. Please select a different model."},
        )
        return False
    finally:
        resolve_future(request.provider_acceptance, False)
        reject_future(request.start_ack, error)
