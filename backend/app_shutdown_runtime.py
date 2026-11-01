"""Runtime-owner shutdown helpers used by the application lifespan."""

from __future__ import annotations


async def shutdown_fork_owners():
    """Join shared forks, then their checkpoint-specific wake owner."""
    from core.checkpoint_runtime import shutdown_checkpoint_wakes
    from core.fork_runtime import shutdown_forks

    try:
        await shutdown_forks()
    finally:
        await shutdown_checkpoint_wakes()


async def shutdown_devices(logger, pending_cancel, run_cleanup_owner):
    """Join the Devices runtime before later lifecycle owners tear down."""
    from core.devices_runtime import runtime as devices_runtime

    failure, cancelled = await run_cleanup_owner(
        "devices listener", devices_runtime.disable(), logger,
    )
    return failure, pending_cancel or cancelled


async def shutdown_otlp_listener(logger, pending_cancel, run_cleanup_owner):
    """Stop the loopback measure receiver so no outside tool writes during teardown."""
    from infra.otlp_listener import listener as otlp_listener

    failure, cancelled = await run_cleanup_owner(
        "otlp listener", otlp_listener.stop(), logger,
    )
    return failure, pending_cancel or cancelled
