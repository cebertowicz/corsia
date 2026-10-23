"""Cancellation and joining of lifecycle-owned background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from app_shutdown_join import join_cleanup
from app_startup_runtime import RUNTIME_LOOP_TASKS
from infra import state


async def shutdown_owned_tasks(
    startup_tasks: list[asyncio.Task | None], logger,
):
    """Cancel and join exact background and finite startup tasks, and stop held vendor and app sign-ins."""
    from connections.account_controls import stop_all_signins
    from connections.microsoft.oauth import stop_all_signins as stop_microsoft_signins
    from jobs.corrections_realtime import shutdown as stop_corrections_classifier
    from llm.local_setup import flight_task
    from llm.local_setup_prepare import prepare_task
    from tools.agent_shell_tools import prefetch_task as agent_tool_prefetch_task
    from tools.mcp_oauth_flow import stop_all as stop_app_signins

    owned_tasks = [
        *((label, getattr(state, attr, None)) for label, attr in RUNTIME_LOOP_TASKS),
        (
            "GitHub ownership login refresh",
            getattr(state, "_github_ownership_login_task", None),
        ),
        ("local model setup", flight_task()),
        ("local model pick", prepare_task()),
        ("agent tool prefetch", agent_tool_prefetch_task()),
        *zip(
            ("provider auto-start", "cloud discovery", "network discovery",
             "harness discovery", "continuity drive"),
            startup_tasks,
            strict=True,
        ),
    ]
    exact_tasks: list[Awaitable[object]] = []
    exact_labels: list[str] = []
    for label, task in owned_tasks:
        if (
            task is None
            or not hasattr(task, "__await__")
            or any(task is exact for exact in exact_tasks)
        ):
            continue
        exact_tasks.append(task)
        exact_labels.append(label)
        if not task.done():
            task.cancel()
    # A held vendor login must not outlive the backend: Codex's would keep its one callback port.
    exact_tasks.append(stop_all_signins())
    exact_labels.append("account sign-in")
    exact_tasks.append(stop_microsoft_signins())
    exact_labels.append("Microsoft account sign-in")
    # Likewise a pending connected-app sign-in's loopback listener and its expiry timer.
    exact_tasks.append(stop_app_signins())
    exact_labels.append("connected-app sign-in")
    exact_tasks.append(stop_corrections_classifier())
    exact_labels.append("corrections classifier")
    results, pending_cancel = await join_cleanup(exact_tasks)
    for label, result in zip(exact_labels, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError,
        ):
            logger.warning(f"shutdown: {label} cleanup failed: {result}")
    return pending_cancel
