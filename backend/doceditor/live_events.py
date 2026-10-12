"""Document product events: identities and outcome kinds only, never text or paths."""
from __future__ import annotations

import asyncio
from functools import partial


def emit(action: str, **properties) -> None:
    """Record off the pump; analytics never blocks or fails a document operation."""
    from infra.product_events import emit_product_event
    record = partial(emit_product_event, "documents", action, properties=properties)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        record()
        return
    loop.run_in_executor(None, record)


def lane_outcome(lane, operation, outcome: str, *, detached: bool = False,
                 server_saved: bool | None = None) -> None:
    """The lane's settle point reports each operation outcome here, once per outcome."""
    from doceditor.live_lane import Operation
    if not isinstance(operation, Operation):
        return  # Setup, recovery and choices are not author operations.
    properties = {"artifact_id": lane.artifact_id, "store_id": lane.live.store.store_id,
                  "operation": operation.kind, "outcome": outcome, "detached": detached}
    if server_saved is not None:
        properties["server_saved"] = server_saved
    emit("lane_outcome", **properties)


def region_after_commit(connection, outcome: str, artifact_id: str, w: float, h: float, **properties) -> None:
    """Report a placed or moved image region once its write commits: identity and shape only."""
    from infra.write_effects import after_commit
    shape = "point" if w <= 0 and h <= 0 else "box"
    after_commit(connection, lambda: emit("image_region_comment", artifact_id=artifact_id, outcome=outcome,
                                          shape=shape, **properties))


def marked_copy_failed(stage: str, error: BaseException) -> None:
    """A marked-up image could not be drawn; the error's type stands in for its text, which names paths."""
    emit("image_region_marked_copy", outcome="failed", stage=stage, error=type(error).__name__)
