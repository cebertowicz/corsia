"""Diagnosis runs only: on SIGUSR1 write the stack of every pending asyncio task of the process.

e2e.yml puts this directory on PYTHONPATH for a filtered dispatch, so a request that hangs with an idle event loop
shows what it is awaiting. Nothing else imports this.
"""
import asyncio
import os
import signal
import time

_TARGET = os.environ.get("CORSIA_DIAG_TASKS_FILE")


def _write_await_chain(awaitable, out):
    """Each coroutine frame the task is suspended in, outermost first, down to what it waits on."""
    while awaitable is not None:
        frame = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "gi_frame", None)
        if frame is not None:
            out.write(f"  {frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}\n")
        else:
            out.write(f"  waiting on {awaitable!r}\n")
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "gi_yieldfrom", None)


def _dump(_signum, _frame):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with open(_TARGET, "a") as out:
        out.write(f"=== {time.strftime('%H:%M:%S')} pid={os.getpid()}\n")
        for task in asyncio.all_tasks(loop):
            if not task.done():
                out.write(f"--- {task.get_name()}\n")
                _write_await_chain(task.get_coro(), out)


if _TARGET:
    signal.signal(signal.SIGUSR1, _dump)
