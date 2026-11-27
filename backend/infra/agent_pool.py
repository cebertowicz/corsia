"""Parent-owned, bounded subprocess pool; currently supports only a fixture call."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import time
import uuid
from pathlib import Path

from infra.agent_pool_child import MAX_FRAME


class PoolError(RuntimeError):
    pass


class PoolClosed(PoolError):
    pass


class WorkerFailed(PoolError):
    pass


def core_ceiling() -> int:
    return max(1, min(6, (os.cpu_count() or 1) // 4))


def _environment(idle_seconds: float) -> dict[str, str]:
    # An allowlist prevents both known and newly named bearer credentials crossing.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONUNBUFFERED"] = "1"
    # The parent's timers enforce idle expiry.
    if os.environ.get("CORSIA_TEST_MODE") == "1":
        env["CORSIA_TEST_MODE"] = "1"
    return env


def _encode(value: dict) -> bytes:
    try:
        data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be JSON") from exc
    if not data or len(data) > MAX_FRAME:
        raise ValueError("invalid frame size")
    return struct.pack("!I", len(data)) + data


async def _read_frame(stream: asyncio.StreamReader) -> dict:
    try:
        size = struct.unpack("!I", await stream.readexactly(4))[0]
        if not 0 < size <= MAX_FRAME:
            raise WorkerFailed("invalid worker frame size")
        result = json.loads(await stream.readexactly(size))
        if not isinstance(result, dict):
            raise WorkerFailed("invalid worker frame")
        return result
    except (asyncio.IncompleteReadError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerFailed("worker exited or sent malformed frame") from exc


class AgentPool:
    # Assumption pending workload evidence: 30 seconds permits short bursts to reuse
    # a child while putting a finite bound on how long idle memory is retained.
    def __init__(self, *, limit: int | None = None, idle_seconds: float = 30, cancel_grace: float = 0.2, shutdown_timeout: float = 2):
        ceiling = core_ceiling()
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= ceiling):
            raise ValueError("limit exceeds core ceiling")
        if idle_seconds <= 0 or cancel_grace < 0 or shutdown_timeout <= 0:
            raise ValueError("invalid lifecycle duration")
        self.limit = limit or ceiling
        self.idle_seconds = idle_seconds
        self.cancel_grace = cancel_grace
        self.shutdown_timeout = shutdown_timeout
        self._condition = asyncio.Condition()
        self._idle: list[asyncio.subprocess.Process] = []
        self._idle_timers: dict[asyncio.subprocess.Process, asyncio.Task] = {}
        self._timer_tasks: set[asyncio.Task] = set()
        self._pending_cleanup: set[asyncio.Task] = set()
        self._reapers: dict[asyncio.subprocess.Process, asyncio.Task] = {}
        self._workers: set[asyncio.subprocess.Process] = set()
        self._priorities: dict[asyncio.subprocess.Process, str] = {}
        self._io_tasks: dict[asyncio.subprocess.Process, asyncio.Task] = {}
        self._reserved = 0
        self._closed = False

    async def _acquire(self):
        async with self._condition:
            while True:
                if self._closed:
                    raise PoolClosed("pool closed")
                while self._idle:
                    proc = self._idle.pop()
                    timer = self._idle_timers.pop(proc, None)
                    if timer is not None:
                        timer.cancel()
                    if proc.returncode is None:
                        return proc
                    # Reap even an exited idle process to close its pipe transports.
                    self._track_cleanup(self._discard(proc))
                if len(self._workers) + self._reserved < self.limit:
                    self._reserved += 1
                    break
                await self._condition.wait()
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-m", "infra.agent_pool_child",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            env=_environment(self.idle_seconds),
        ))
        try:
            proc = await asyncio.shield(spawn)
        except BaseException:
            # Do not hold the caller past its deadline while the OS spawn finishes.
            self._track_cleanup(self._finish_abandoned_spawn(spawn))
            raise
        # Register before yielding so shutdown and cancellation cannot miss a spawned child.
        self._reserved -= 1
        self._workers.add(proc)
        try:
            async with self._condition:
                self._condition.notify_all()
            if self._closed:
                raise PoolClosed("pool closed")
            ready = await self._run_io(proc, _read_frame(proc.stdout))
            if set(ready) != {"version", "type", "priority"} or ready["version"] != 1 or ready["type"] != "ready" or ready["priority"] not in ("lowered", "permission_denied", "failed"):
                raise WorkerFailed("invalid worker readiness frame")
            self._priorities[proc] = ready["priority"]
            if self._closed:
                raise PoolClosed("pool closed")
            return proc
        except asyncio.CancelledError as exc:
            self._track_cleanup(self._discard(proc))
            task = asyncio.current_task()
            if self._closed and task is not None and not task.cancelling():
                raise PoolClosed("pool closed during startup") from exc
            raise
        except BaseException:
            self._track_cleanup(self._discard(proc))
            raise

    def _track_cleanup(self, coro):
        task = asyncio.create_task(coro)
        self._pending_cleanup.add(task)
        def finished(done):
            self._pending_cleanup.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                logging.getLogger(__name__).error("Worker cleanup failed: %s", type(error).__name__)
        task.add_done_callback(finished)

    async def _finish_abandoned_spawn(self, spawn):
        try:
            proc = await spawn
            # A late child occupies capacity even if its first reap fails.
            self._workers.add(proc)
            await self._discard(proc)
        finally:
            async with self._condition:
                self._reserved -= 1
                self._condition.notify_all()

    async def _run_io(self, proc, operation):
        if self._closed:
            operation.close()
            raise WorkerFailed("pool closed during call")
        task = asyncio.create_task(operation)
        self._io_tasks[proc] = task
        try:
            return await task
        finally:
            if self._io_tasks.get(proc) is task:
                self._io_tasks.pop(proc, None)

    async def _discard(self, proc):
        task = self._reapers.get(proc)
        if task is None:
            task = asyncio.create_task(self._reap(proc))
            self._reapers[proc] = task
            def finished(done):
                if self._reapers.get(proc) is done:
                    self._reapers.pop(proc, None)
                if not done.cancelled() and (error := done.exception()) is not None:
                    logging.getLogger(__name__).error("Worker reap failed: %s", type(error).__name__)
            task.add_done_callback(finished)
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._reapers.get(proc) is task:
                self._reapers.pop(proc, None)

    def _idle_timer_done(self, task):
        self._timer_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.getLogger(__name__).error("Worker idle cleanup failed: %s", type(error).__name__)

    async def _expire_idle(self, proc):
        await asyncio.sleep(self.idle_seconds)
        async with self._condition:
            if proc not in self._idle:
                return
            self._idle.remove(proc)
            self._idle_timers.pop(proc, None)
        await self._discard(proc)

    async def _reap(self, proc):
        io_task = self._io_tasks.get(proc)
        if io_task is not None:
            io_task.cancel()
            await asyncio.gather(io_task, return_exceptions=True)
        if proc.returncode is None:
            try:
                proc.stdin.write(_encode({"version": 1, "type": "cancel"}))
                await asyncio.wait_for(proc.stdin.drain(), self.cancel_grace)
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
                pass
        # Discard queued input using the public transport API, including if a
        # child has stopped reading; then drain output without retaining it.
        if not proc.stdin.is_closing():
            proc.stdin.transport.abort()
        async def drain_output():
            while await proc.stdout.read(65536):
                pass
        drainer = asyncio.create_task(drain_output())
        try:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), self.cancel_grace)
                except asyncio.TimeoutError:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), self.cancel_grace)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
            await asyncio.wait_for(proc.wait(), self.shutdown_timeout)
            await asyncio.wait_for(drainer, self.shutdown_timeout)
            try:
                await asyncio.wait_for(proc.stdin.wait_closed(), self.shutdown_timeout)
            except (BrokenPipeError, ConnectionError):
                pass
        finally:
            if not drainer.done():
                drainer.cancel()
                await asyncio.gather(drainer, return_exceptions=True)
        async with self._condition:
            self._workers.discard(proc)
            self._priorities.pop(proc, None)
            if proc in self._idle:
                self._idle.remove(proc)
            self._condition.notify_all()
        self._reapers.pop(proc, None)

    async def call(self, operation: str, payload, *, agent_id: str, thread_id: str,
                   queue_timeout: float, execution_timeout: float):
        if operation != "fixture.echo":
            raise ValueError("unknown operation")
        if not all(isinstance(v, str) and v for v in (agent_id, thread_id)):
            raise ValueError("identity required")
        if queue_timeout <= 0 or execution_timeout <= 0:
            raise ValueError("deadlines must be positive")
        identity = {"call_id": uuid.uuid4().hex, "agent_id": agent_id, "thread_id": thread_id}
        frame = _encode({"version": 1, "type": "call", "identity": identity, "operation": operation, "payload": payload})
        from infra.request_timing import record_worker_wait
        started = time.monotonic()
        try:
            async with asyncio.timeout(queue_timeout):
                proc = await self._acquire()
        except asyncio.CancelledError:
            record_worker_wait(time.monotonic() - started, "cancelled")
            raise
        except asyncio.TimeoutError:
            record_worker_wait(time.monotonic() - started, "timed_out")
            raise
        except BaseException:
            record_worker_wait(time.monotonic() - started, "failed")
            raise
        record_worker_wait(time.monotonic() - started, "assigned")
        reusable = False
        try:
            async def exchange():
                proc.stdin.write(frame)
                await proc.stdin.drain()
                result = await _read_frame(proc.stdout)
                if result.get("version") != 1 or result.get("type") != "result" or result.get("identity") != identity or "payload" not in result:
                    raise WorkerFailed("invalid worker response")
                return result["payload"]
            value = await asyncio.wait_for(self._run_io(proc, exchange()), execution_timeout)
            reusable = True
            return value
        except (BrokenPipeError, ConnectionError) as exc:
            raise WorkerFailed("worker exited") from exc
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if self._closed and task is not None and not task.cancelling():
                raise WorkerFailed("pool closed during call") from exc
            raise
        finally:
            try:
                if reusable and proc.returncode is None and not self._closed:
                    async with self._condition:
                        if not self._closed:
                            self._idle.append(proc)
                            timer = asyncio.create_task(self._expire_idle(proc))
                            self._idle_timers[proc] = timer
                            self._timer_tasks.add(timer)
                            timer.add_done_callback(self._idle_timer_done)
                            self._condition.notify_all()
                        else:
                            reusable = False
                else:
                    reusable = False
                if not reusable:
                    self._track_cleanup(self._discard(proc))
            except BaseException:
                self._track_cleanup(self._discard(proc))
                raise

    async def close(self):
        async with self._condition:
            self._closed = True
            workers = list(self._workers)
            timers = list(self._timer_tasks)
            for timer in timers:
                timer.cancel()
            self._idle_timers.clear()
            self._idle.clear()
            self._condition.notify_all()
        try:
            async with asyncio.timeout(self.shutdown_timeout):
                await asyncio.gather(*timers, return_exceptions=True)
                await asyncio.gather(*(self._discard(p) for p in workers))
                async with self._condition:
                    await self._condition.wait_for(lambda: self._reserved == 0 and not self._workers)
                if self._pending_cleanup:
                    await asyncio.gather(*(asyncio.shield(t) for t in list(self._pending_cleanup)))
        except asyncio.TimeoutError as exc:
            raise PoolError("pool shutdown timed out; cleanup remains pending") from exc


_pool: AgentPool | None = None


def start() -> AgentPool:
    global _pool
    if _pool is None:
        _pool = AgentPool()
    elif _pool._closed:
        raise PoolClosed("previous pool has not completed shutdown")
    return _pool


async def shutdown() -> None:
    global _pool
    pool = _pool
    if pool is not None:
        await pool.close()
        if _pool is pool:
            _pool = None
