"""Reader termination proof with controlled expiry rather than racing clocks."""

import asyncio

from subagents.codex_app_server import CodexAppServerSession


async def test_terminate_is_bounded_when_reader_ignores_first_cancellation(monkeypatch):
    session = CodexAppServerSession(binary="codex", task=None)
    first_cancel = asyncio.Event()
    attempts = []
    real_wait = asyncio.wait

    class Proc:
        returncode = None
        kill_calls = 0

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def resistant_reader():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancel.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    async def controlled_wait(tasks, *, timeout):
        assert tasks == {reader}
        attempts.append((reader.cancelling(), timeout))
        if len(attempts) == 1:
            # Expire precisely after the first cancellation was resisted.
            await first_cancel.wait()
            return set(), tasks
        # The second cancellation must complete the reader, without another
        # wall-clock timer racing the test's single hang watchdog.
        return await real_wait(tasks)

    proc = Proc()
    reader = asyncio.create_task(resistant_reader())
    await asyncio.sleep(0)
    session._proc = proc
    session._reader = reader
    monkeypatch.setattr(asyncio, "wait", controlled_wait)
    try:
        await asyncio.wait_for(session.terminate(timeout=0.01), timeout=1)
        assert attempts == [(1, 0.01), (2, 0.01)]
        assert first_cancel.is_set()
        assert reader.done()
        assert proc.kill_calls == 1
    finally:
        reader.cancel()
        await asyncio.sleep(0)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
