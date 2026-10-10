from __future__ import annotations

import asyncio
import logging

import pytest


@pytest.mark.asyncio
async def test_cancelled_fast_forward_logs_failed_worker_settlement(
    monkeypatch,
    caplog,
) -> None:
    """Cancellation remains authoritative while worker failure stays visible."""
    from tools import shell_fast_forward

    class _Owner:
        async def terminate_turn(self, *_args, **_kwargs):
            return True

    work = asyncio.get_running_loop().create_future()
    work.set_exception(RuntimeError("worker settlement failed"))
    monkeypatch.setattr(shell_fast_forward, "PROCESS_OWNER", _Owner())
    cancellation = asyncio.CancelledError()

    with caplog.at_level(logging.WARNING, logger="tools.shell_fast_forward"):
        with pytest.raises(asyncio.CancelledError) as caught:
            await shell_fast_forward.settle_cancelled_fast_forward(
                work,
                thread_id="thread-1",
                turn_id="turn-1",
                cancellation=cancellation,
            )

    assert caught.value is cancellation
    assert "worker settlement failed" in caplog.text
    assert "thread-1" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_fast_forward_preserves_cancellation_when_stop_fails(
    monkeypatch,
    caplog,
) -> None:
    """A process-stop failure is evidence, never a replacement terminal state."""
    from tools import shell_fast_forward

    class _Owner:
        async def terminate_turn(self, *_args, **_kwargs):
            raise RuntimeError("process stop failed")

    work = asyncio.get_running_loop().create_future()
    work.set_result(None)
    cancellation = asyncio.CancelledError()
    monkeypatch.setattr(shell_fast_forward, "PROCESS_OWNER", _Owner())

    with caplog.at_level(logging.WARNING, logger="tools.shell_fast_forward"):
        with pytest.raises(asyncio.CancelledError) as caught:
            await shell_fast_forward.settle_cancelled_fast_forward(
                work,
                thread_id="thread-2",
                turn_id="turn-2",
                cancellation=cancellation,
            )

    assert caught.value is cancellation
    assert "process stop failed" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_fast_forward_preserves_original_after_worker_cancels(
    monkeypatch,
    caplog,
) -> None:
    """A cancelled worker cannot replace the caller's cancellation identity."""
    from tools import shell_fast_forward

    class _Owner:
        async def terminate_turn(self, *_args, **_kwargs):
            return True

    work = asyncio.get_running_loop().create_future()
    work.cancel()
    cancellation = asyncio.CancelledError("original cancellation")
    monkeypatch.setattr(shell_fast_forward, "PROCESS_OWNER", _Owner())

    with caplog.at_level(logging.WARNING, logger="tools.shell_fast_forward"):
        with pytest.raises(asyncio.CancelledError) as caught:
            await shell_fast_forward.settle_cancelled_fast_forward(
                work,
                thread_id="thread-3",
                turn_id="turn-3",
                cancellation=cancellation,
            )

    assert caught.value is cancellation
    assert "CancelledError" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_fast_forward_joins_worker_after_second_cancellation(
    monkeypatch,
    caplog,
) -> None:
    """Repeated caller cancellation cannot abandon the owned worker."""
    from tools import shell_fast_forward

    work = asyncio.get_running_loop().create_future()

    class _Owner:
        async def terminate_turn(self, *_args, **_kwargs):
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
            asyncio.get_running_loop().call_later(0.01, work.set_result, None)
            return True

    cancellation = asyncio.CancelledError("original cancellation")
    monkeypatch.setattr(shell_fast_forward, "PROCESS_OWNER", _Owner())

    with caplog.at_level(logging.WARNING, logger="tools.shell_fast_forward"):
        with pytest.raises(asyncio.CancelledError) as caught:
            await shell_fast_forward.settle_cancelled_fast_forward(
                work,
                thread_id="thread-4",
                turn_id="turn-4",
                cancellation=cancellation,
            )

    assert caught.value is cancellation
    assert work.done() and work.result() is None
    assert "CancelledError" in caplog.text
