"""Runtime-owner shutdown helpers used by the application lifespan."""

from __future__ import annotations


async def shutdown_devices(logger, pending_cancel, run_cleanup_owner):
    """Join the Devices runtime before later lifecycle owners tear down."""
    from core.devices_runtime import runtime as devices_runtime

    failure, cancelled = await run_cleanup_owner(
        "devices listener", devices_runtime.disable(), logger,
    )
    return failure, pending_cancel or cancelled
