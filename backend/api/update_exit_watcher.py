"""Settle a failed update lifecycle while its original backend is alive."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from loguru import logger

_UPDATE_EXIT_TASKS: set[asyncio.Task] = set()


def watch_update_process(process, operation_id: str, log_path: Path) -> None:
    """Watch without letting the child's lifetime hold backend shutdown open."""
    loop = asyncio.get_running_loop()

    def report(returncode: int) -> None:
        # 200 marks possible teardown. An untrappable signal cannot convey
        # that boundary, so its outcome is also uncertain.
        if returncode <= 0 or returncode == 200:
            return
        task = asyncio.create_task(_settle_exited_update(operation_id, returncode, log_path))
        _UPDATE_EXIT_TASKS.add(task)
        task.add_done_callback(_UPDATE_EXIT_TASKS.discard)
        task.add_done_callback(_log_update_watch_failure)

    def wait_for_child() -> None:
        try:
            returncode = process.wait()
        except Exception:
            logger.exception("Update lifecycle child wait failed")
            try:
                returncode = process.poll()
            except Exception:
                logger.exception("Update lifecycle child status unknown; teardown remains uncertain")
                return
            if returncode is None:
                logger.warning("Update lifecycle child still running; teardown remains uncertain")
                return
        try:
            loop.call_soon_threadsafe(report, returncode)
        except RuntimeError:
            logger.debug("Update lifecycle exit arrived after backend loop closed")

    threading.Thread(target=wait_for_child, name="update-lifecycle-wait", daemon=True).start()


def _log_update_watch_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.opt(exception=task.exception()).error("Update lifecycle failure settlement failed")


async def _settle_exited_update(operation_id: str, returncode: int, log_path: Path) -> None:
    """Settle an update whose lifecycle failed before possible teardown."""
    from automation.restart import abort_restart_control
    from infra.update_prepare_output import terminal_update_summary

    def failure_reason() -> str:
        try:
            lines = log_path.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        lines = [line for line in lines if not line.startswith("Lifecycle ")]
        return terminal_update_summary("\n".join(lines), None) if lines else f"restart script exited: {returncode}"

    reason = await asyncio.to_thread(failure_reason)
    await abort_restart_control(
        operation_id, error=reason, failure_code="launch_failed",
        failure_detail=json.dumps({"exit_code": returncode, "log_path": str(log_path)}),
    )
