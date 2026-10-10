"""The live apply waits one full loop pass: due timers and the tasks they wake run first."""
import asyncio

import pytest

from doceditor.live_document import yield_loop_pass


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 3])
async def test_loop_pass_runs_due_timers_and_the_tasks_they_wake(depth):
    loop = asyncio.get_running_loop()
    woken, seen = asyncio.Event(), []
    async def sleeper():
        await woken.wait()
        seen.append("task")
    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    def due():
        seen.append("timer")
        woken.set()
    loop.call_at(loop.time() - 1, due)
    async def nested(level):
        if level:
            return await nested(level - 1)
        await yield_loop_pass()
    await nested(depth)
    assert seen == ["timer", "task"]
    await task
