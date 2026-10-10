"""After-commit driver for the FIFO owner's one Turn-admission outcome."""

from __future__ import annotations

from infra.failed_wake_surface import drive_failed_wake_owners
from infra.send_broadcast import broadcast_message_rows
from infra.send_types import InboxQueuedInput, SendResult
from infra.thread_input_types import FailedInputBatch, HeldAdmission, TurnAdmission


async def drive_turn_admission(
    admission: TurnAdmission,
) -> SendResult | InboxQueuedInput | None:
    """Execute a claimed Turn or publish the exact visibly failed FIFO prefix."""
    if isinstance(admission, HeldAdmission):
        from thread.model_pending import drive_pending_model_switch

        # A held prefix stays queued, but a pending switch may release it.
        await drive_pending_model_switch(admission.thread_id)
        return None
    if isinstance(admission, FailedInputBatch):
        await broadcast_message_rows(admission.thread_id, admission.message_ids)
        from thread.model_pending import drive_pending_model_switch

        # Ending the source prefix may have made its destination runnable.
        await drive_pending_model_switch(admission.thread_id)
        await drive_failed_wake_owners(admission.message_ids)
        return None
    from infra.thread_execution import drive_claimed_batch

    return await drive_claimed_batch(admission)
