"""Own the Update lifecycle child's exit while the backend is running."""

from __future__ import annotations

import asyncio
from threading import Thread

from api.restart_shell import PRE_STOP_UPDATE_REFUSED

_UPDATE_EXIT_TASKS: set[asyncio.Task] = set()


def own_update_child_exit(process, operation_id: str) -> None:
    """Settle a confirmed pre-stop refusal without holding backend shutdown."""
    loop = asyncio.get_running_loop()

    def on_exit() -> None:
        """Wait for the child and deliver a confirmed refusal to the event loop."""
        if process.wait() != PRE_STOP_UPDATE_REFUSED:
            return

        def schedule() -> None:
            """Keep the settlement task alive until it finishes."""
            task = loop.create_task(settle_update_child_refusal(operation_id))
            _UPDATE_EXIT_TASKS.add(task)
            task.add_done_callback(_UPDATE_EXIT_TASKS.discard)

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            # The backend has shut down; startup owns any remaining operation.
            pass

    Thread(target=on_exit, name="update-child-exit", daemon=True).start()


async def settle_update_child_refusal(operation_id: str) -> None:
    """Release only this Update if it has not advanced past readiness."""
    from automation.restart import abort_restart_control

    await abort_restart_control(
        operation_id,
        error="Update failed before restart; see the update log.",
        failure_code="update_refused",
        pre_stop_only=True,
    )
