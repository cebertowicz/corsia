"""Async bridge for SQLite DB work."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from loguru import logger

from infra.db_connection import mark_thread_lane

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db")
# Read-only lanes, so a page read never queues behind writes or report scans on
# the default pool, and a report scan holds at most its own lane's threads.
_read_pool = ThreadPoolExecutor(
    max_workers=6, thread_name_prefix="db-read", initializer=mark_thread_lane, initargs=("read",),
)
_bulk_pool = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="db-bulk", initializer=mark_thread_lane, initargs=("bulk",),
)
_admission = threading.Condition()
_active_jobs = 0
_pool_closing = False


@dataclass
class _TimedResult:
    value: object
    started_at: float
    finished_at: float


def _run_db_job(fn, scope: str):
    from infra.database import db_job_boundary
    from infra.db_connection import job_cursors

    global _active_jobs
    with _admission:
        while _pool_closing:
            _admission.wait()
        _active_jobs += 1
    try:
        with db_job_boundary(scope), job_cursors(scope):
            return fn()
    finally:
        with _admission:
            _active_jobs -= 1
            _admission.notify_all()


def _run_timed_db_job(fn, scope: str) -> _TimedResult:
    """Run one database job while measuring only the worker execution window."""
    started_at = time.perf_counter()
    try:
        value = _run_db_job(fn, scope)
    except BaseException as error:
        error._corsia_db_timing = (started_at, time.perf_counter())
        raise
    return _TimedResult(value, started_at, time.perf_counter())


async def run_in_db(fn, *args, **kwargs):
    """Run a sync SQLite job on the DB pool; see :func:`_run_shielded`."""
    return await _run_shielded(_pool, None, fn, *args, **kwargs)


async def run_read(fn, *args, **kwargs):
    """Run a short page read on the read lane, whose connections are ``query_only``.

    Only for a job that never writes, end to end: any write there raises.
    """
    return await _run_shielded(_read_pool, "read", fn, *args, **kwargs)


async def run_bulk(fn, *args, **kwargs):
    """Run a long report scan on the bulk lane, which may write TEMP tables but never the main database."""
    return await _run_shielded(_bulk_pool, "bulk", fn, *args, **kwargs)


async def run_in_db_unless_owned(owned_db, fn, /, *args, **kwargs):
    """Run a sync SQLite job on the DB pool, or inline when the caller passed the connection it owns.

    A caller-owned connection belongs to the caller's thread and transaction and cannot move to
    a pool thread, so only ``db=None`` (production) leaves the event loop.
    """
    if owned_db is not None:
        return fn(*args, **kwargs)
    return await run_in_db(fn, *args, **kwargs)


async def run_off_db(fn, *args, **kwargs):
    """Run a sync job that also waits on something slower than SQLite (a guidance save's Git lock
    and sweep) with the same contract, in the default executor so the wait holds no DB worker."""
    return await _run_shielded(None, None, fn, *args, **kwargs)


async def _run_shielded(executor, lane, fn, /, *args, **kwargs):
    """Run a sync function and never outlive its caller after cancellation.

    ``run_in_executor`` cancellation only cancels the asyncio wrapper; a worker
    already executing SQLite code continues in the thread pool. Letting the
    awaiting task report cancellation before that exact worker finishes breaks
    shutdown/restart isolation: the old process can mutate newly recovered
    delivery state after its task registry is empty. Shield the worker future,
    join it on cancellation, consume its result, then propagate cancellation.
    """
    loop = asyncio.get_running_loop()
    scope = getattr(fn, "__qualname__", type(fn).__name__)
    if args or kwargs:
        fn = partial(fn, *args, **kwargs)
    submitted_at = time.perf_counter()
    future = loop.run_in_executor(executor, _run_timed_db_job, fn, scope)
    try:
        timed = await asyncio.shield(future)
        from infra.request_timing import add_db_timing

        add_db_timing(
            timed.started_at - submitted_at,
            timed.finished_at - timed.started_at,
            lane,
        )
        return timed.value
    except BaseException as error:
        if timing := getattr(error, "_corsia_db_timing", None):
            from infra.request_timing import add_db_timing

            add_db_timing(timing[0] - submitted_at, timing[1] - timing[0], lane)
        if not isinstance(error, asyncio.CancelledError):
            raise
        cancelled = error
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                # Repeated cancellation still cannot let the executor worker
                # escape the lifetime of the task that submitted it.
                continue
            except BaseException:
                # The caller's cancellation remains authoritative once the
                # exact worker has finished, even if that worker also failed.
                break
        if not future.cancelled():
            try:
                future.result()
            except BaseException:
                # Cancellation remains the caller-visible result, but consume a
                # worker failure so it cannot surface later as an unobserved
                # future exception.
                pass
        raise cancelled


def close_pool(close, timeout: float = 10.0) -> None:
    """Close pooled connections without admitting a DB job across that boundary.

    A queue barrier alone is racy: a job submitted after its barrier can start
    while the caller closes a connection. Holding admission until ``close``
    finishes makes pool replacement and shutdown one atomic ownership change.
    """
    global _pool_closing
    with _admission:
        while _pool_closing:
            _admission.wait()
        _pool_closing = True
        if not _admission.wait_for(lambda: _active_jobs == 0, timeout=timeout):
            _pool_closing = False
            _admission.notify_all()
            raise TimeoutError("DB jobs did not drain before pool close")
    try:
        close()
    finally:
        with _admission:
            _pool_closing = False
            _admission.notify_all()


def shutdown():
    """Shut down the DB thread pool and its read and bulk lanes."""
    logger.info("db_async: shutting down thread pool (3s timeout)")

    done = threading.Event()

    def _do_shutdown():
        pools = (_pool, _read_pool, _bulk_pool)
        for pool in pools:  # cancel every lane's queue before waiting on any of them
            pool.shutdown(wait=False, cancel_futures=True)
        for pool in pools:
            pool.shutdown(wait=True)
        done.set()

    t = threading.Thread(target=_do_shutdown, daemon=True)
    t.start()

    if not done.wait(timeout=3.0):
        logger.warning(
            "db_async: pool shutdown timed out after 3s — proceeding with exit"
        )
    else:
        logger.info("db_async: thread pool shut down cleanly")
