"""Parent-owned, bounded subprocess pool; currently supports only a fixture call."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
import json
import logging
import os
from pathlib import Path
import struct
import sys
import time
import uuid

from infra.agent_pool_child import MAX_FRAME
from infra.agent_pool_diagnostics import capture_stderr


class PoolError(RuntimeError):
    pass


class PoolClosed(PoolError):
    pass


class WorkerFailed(PoolError):
    pass


def core_ceiling() -> int:
    # A quarter of cores gives the design's two on eight; the cap gives six on 32.
    # Two broad-import fixtures used ~500 MiB USS, not a production memory bound.
    return max(1, min(6, (os.cpu_count() or 1) // 4))


def _environment() -> dict[str, str]:
    # An allowlist prevents both known and newly named bearer credentials crossing.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONUNBUFFERED"] = "1"
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


class WorkerState(Enum):
    STARTING = auto()
    BUSY = auto()
    IDLE = auto()
    REAPING = auto()
    REAP_FAILED = auto()


@dataclass
class Worker:
    # STARTING -> BUSY -> IDLE -> BUSY; any live state -> REAPING.
    # A failed reap retains capacity as REAP_FAILED; shutdown can retry it.
    proc: asyncio.subprocess.Process
    state: WorkerState = WorkerState.STARTING
    io: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    reaper: asyncio.Task | None = None
    stderr: asyncio.Task | None = None
    diagnostic: str = ""
    error_detail: str = ""
    error_reported: asyncio.Event = field(default_factory=asyncio.Event)


class AgentPool:
    # A short idle window limits retained memory; if too short, later calls respawn.
    # Three 0.2s grace windows fit within the 2s shutdown budget; if too short,
    # termination escalates and unfinished cleanup is reported rather than forgotten.
    def __init__(self, *, limit: int | None = None, idle_seconds: float = 1, cancel_grace: float = 0.2, shutdown_timeout: float = 2):
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
        self._workers: dict[asyncio.subprocess.Process, Worker] = {}
        self._pending_cleanup: set[asyncio.Task] = set()
        self._reserved = 0
        self._closed = False

    def _register(self, proc) -> Worker:
        worker = Worker(proc)
        self._workers[proc] = worker
        worker.stderr = asyncio.create_task(capture_stderr(worker))
        return worker

    async def _acquire(self):
        async with self._condition:
            while True:
                if self._closed:
                    raise PoolClosed("pool closed")
                for worker in list(self._workers.values()):
                    if worker.state is WorkerState.IDLE:
                        worker.timer.cancel()
                        worker.timer = None
                        if worker.proc.returncode is None:
                            worker.state = WorkerState.BUSY
                            return worker
                        worker.state = WorkerState.REAPING
                        self._track_cleanup(self._discard(worker))
                if len(self._workers) + self._reserved < self.limit:
                    self._reserved += 1
                    break
                await self._condition.wait()
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-m", "infra.agent_pool_child", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_environment(),
        ))
        try:
            proc = await asyncio.shield(spawn)
        except BaseException:
            self._track_cleanup(self._finish_abandoned_spawn(spawn))
            raise
        self._reserved -= 1
        worker = self._register(proc)
        try:
            async with self._condition:
                self._condition.notify_all()
            if self._closed:
                raise PoolClosed("pool closed")
            ready = await self._run_io(worker, _read_frame(proc.stdout))
            if set(ready) != {"version", "type", "priority"} or ready["version"] != 1 or ready["type"] != "ready" or ready["priority"] not in ("lowered", "permission_denied", "failed"):
                raise WorkerFailed("invalid worker readiness frame")
            if self._closed:
                raise PoolClosed("pool closed")
            worker.state = WorkerState.BUSY
            return worker
        except asyncio.CancelledError as exc:
            self._track_cleanup(self._discard(worker))
            task = asyncio.current_task()
            if self._closed and task is not None and not task.cancelling():
                raise PoolClosed("pool closed during startup") from exc
            raise
        except WorkerFailed as exc:
            try:
                detail = await self._worker_error(worker, exc)
                raise detail from exc
            finally:
                self._track_cleanup(self._discard(worker))
        except BaseException:
            self._track_cleanup(self._discard(worker))
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
            worker = self._register(proc)
            await self._discard(worker)
        finally:
            async with self._condition:
                self._reserved -= 1
                self._condition.notify_all()

    async def _run_io(self, worker, operation):
        if self._closed:
            operation.close()
            raise WorkerFailed("pool closed during call")
        worker.io = asyncio.create_task(operation)
        try:
            return await worker.io
        finally:
            worker.io = None

    async def _worker_error(self, worker, exc):
        # A failed protocol exchange may precede the child's exit; never wait
        # indefinitely for its diagnostic, and keep draining in the background.
        signal = asyncio.create_task(worker.error_reported.wait())
        try:
            # Also finish on EOF; bound the wait if a malformed child stays alive.
            await asyncio.wait((signal, worker.stderr), timeout=0.1,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)
        detail = worker.error_detail or worker.diagnostic
        return WorkerFailed(f"{exc}: {detail}" if detail else str(exc))

    async def _discard(self, worker):
        if worker.proc not in self._workers:
            return
        if worker.reaper is None:
            worker.state = WorkerState.REAPING
            worker.reaper = asyncio.create_task(self._reap(worker))
            def finished(done):
                worker.reaper = None
                if not done.cancelled() and (error := done.exception()) is not None:
                    worker.state = WorkerState.REAP_FAILED
                    logging.getLogger(__name__).error("Worker reap failed: %s", type(error).__name__)
            worker.reaper.add_done_callback(finished)
        await asyncio.shield(worker.reaper)

    async def _expire_idle(self, worker):
        try:
            await asyncio.sleep(self.idle_seconds)
            async with self._condition:
                if worker.state is not WorkerState.IDLE:
                    return
                worker.timer = None
                worker.state = WorkerState.REAPING
            await self._discard(worker)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error("Worker idle cleanup failed: %s", type(exc).__name__)

    async def _reap(self, worker):
        proc = worker.proc
        if worker.io is not None:
            worker.io.cancel()
            await asyncio.gather(worker.io, return_exceptions=True)
        if proc.returncode is None:
            try:
                proc.stdin.write(_encode({"version": 1, "type": "cancel"}))
                await asyncio.wait_for(proc.stdin.drain(), self.cancel_grace)
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
                pass
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
            await asyncio.wait_for(asyncio.shield(worker.stderr), self.shutdown_timeout)
            try:
                await asyncio.wait_for(proc.stdin.wait_closed(), self.shutdown_timeout)
            except (BrokenPipeError, ConnectionError):
                pass
        finally:
            if not drainer.done():
                drainer.cancel()
                await asyncio.gather(drainer, return_exceptions=True)
        async with self._condition:
            self._workers.pop(proc, None)
            self._condition.notify_all()

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
        from infra.worker_wait_timing import record_worker_wait
        started = time.monotonic()
        try:
            async with asyncio.timeout(queue_timeout):
                worker = await self._acquire()
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
        proc = worker.proc
        reusable = False
        try:
            async def exchange():
                proc.stdin.write(frame)
                await proc.stdin.drain()
                result = await _read_frame(proc.stdout)
                if result.get("version") != 1 or result.get("type") != "result" or result.get("identity") != identity or "payload" not in result:
                    raise WorkerFailed("invalid worker response")
                return result["payload"]
            value = await asyncio.wait_for(self._run_io(worker, exchange()), execution_timeout)
            reusable = True
            return value
        except (WorkerFailed, BrokenPipeError, ConnectionError) as exc:
            raise await self._worker_error(worker, exc) from exc
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if self._closed and task is not None and not task.cancelling():
                raise WorkerFailed("pool closed during call") from exc
            raise
        finally:
            try:
                async with self._condition:
                    if reusable and proc.returncode is None and not self._closed:
                        worker.state = WorkerState.IDLE
                        worker.timer = asyncio.create_task(self._expire_idle(worker))
                        self._condition.notify_all()
                    else:
                        reusable = False
                if not reusable:
                    self._track_cleanup(self._discard(worker))
            except BaseException:
                self._track_cleanup(self._discard(worker))
                raise

    async def close(self):
        async with self._condition:
            self._closed = True
            workers = list(self._workers.values())
            timers = [worker.timer for worker in workers if worker.timer is not None]
            for timer in timers:
                timer.cancel()
            self._condition.notify_all()
        try:
            async with asyncio.timeout(self.shutdown_timeout):
                await asyncio.gather(*timers, return_exceptions=True)
                await asyncio.gather(*(self._discard(worker) for worker in workers))
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
