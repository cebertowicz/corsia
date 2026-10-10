"""Real SQLite contention must not stall the owning-thread router's event loop.

Exercise ``route_event`` and ``record_artifact_change`` against file-backed SQLite,
observing real ``BEGIN IMMEDIATE`` entry and acquisition through a borrowed handle.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

import pytest

from tests.event_routing_helpers import mk_session, pr_payload, register_pr

_WATCHDOG_BOUND_SECONDS = 2
# This must exceed the watchdog bound: expiry is a harness failure, never a release path.
_HOLDER_FAILSAFE_SECONDS = 8


async def _delivered(*_args: Any, **_kwargs: Any) -> bool:
    """Keep the probe focused on work before downstream thread delivery."""
    return True


class _BeginObservingConnection:
    """Transparent borrowed-connection proxy that observes only real BEGIN calls."""

    def __init__(
        self,
        connection: Any,
        begin_entered: threading.Event,
        begin_acquired: threading.Event,
        record: Callable[[str], None],
    ) -> None:
        self._connection = connection
        self._begin_entered = begin_entered
        self._begin_acquired = begin_acquired
        self._record = record

    def execute(self, statement: str, *args: Any, **kwargs: Any):
        if statement.strip().upper() != "BEGIN IMMEDIATE":
            return self._connection.execute(statement, *args, **kwargs)
        self._record(f"begin-entered-thread-{threading.get_ident()}")
        self._begin_entered.set()
        result = self._connection.execute(statement, *args, **kwargs)
        self._record(f"begin-acquired-thread-{threading.get_ident()}")
        self._begin_acquired.set()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _hold_real_writer(
    path: str,
    ready: threading.Event,
    release: threading.Event,
    expired: threading.Event,
    record: Callable[[str], None],
) -> None:
    """Hold a foreign real write transaction; release is owned by a watchdog."""
    db = sqlite3.connect(path, timeout=0)
    try:
        db.execute("BEGIN IMMEDIATE")
        record(f"foreign-held-thread-{threading.get_ident()}")
        ready.set()
        if not release.wait(timeout=_HOLDER_FAILSAFE_SECONDS):
            record(f"foreign-holder-expired-thread-{threading.get_ident()}")
            expired.set()
        record(f"foreign-release-thread-{threading.get_ident()}")
        db.commit()
    finally:
        release.set()
        db.close()


def _recording_list() -> tuple[list[str], Callable[[str], None]]:
    values: list[str] = []
    lock = threading.Lock()

    def record(value: str) -> None:
        with lock:
            values.append(value)

    return values, record


def _install_real_begin_observer(monkeypatch, begin_entered, begin_acquired, record) -> None:
    """Patch database lookup only; write_transaction and its real connection remain intact."""
    from infra import database

    original_get_db = database.get_db

    def observed_get_db():
        return _BeginObservingConnection(
            original_get_db(), begin_entered, begin_acquired, record
        )

    monkeypatch.setattr(database, "get_db", observed_get_db)


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


@pytest.mark.asyncio
async def test_no_held_writer_services_callback_after_real_begin(
    isolated_db, monkeypatch
):
    """Control: an independent callback is served after an uncontended real BEGIN."""
    from events import router

    owner = mk_session("router-writer-control")
    register_pr("o/r", 441, owner)
    begin_entered = threading.Event()
    begin_acquired = threading.Event()
    callback_served = threading.Event()
    monitor_done = threading.Event()
    order, record = _recording_list()
    failures: list[str] = []
    loop = asyncio.get_running_loop()
    _install_real_begin_observer(monkeypatch, begin_entered, begin_acquired, record)
    monkeypatch.setattr(router.delivery, "deliver_and_resolve_to_thread", _delivered)

    def callback() -> None:
        record("callback-served")
        callback_served.set()

    def monitor() -> None:
        if not begin_entered.wait(timeout=_WATCHDOG_BOUND_SECONDS):
            failures.append("real BEGIN was never entered")
        loop.call_soon_threadsafe(callback)
        if not begin_acquired.wait(timeout=_WATCHDOG_BOUND_SECONDS):
            failures.append("uncontended real BEGIN never acquired")
        if not callback_served.wait(timeout=_WATCHDOG_BOUND_SECONDS):
            failures.append("callback was not served after uncontended BEGIN")
        monitor_done.set()

    observer = threading.Thread(target=monitor, daemon=True)
    observer.start()
    result = await router.route_event(
        "webhook.github.pr_opened", pr_payload("o/r", 441)
    )
    await asyncio.to_thread(observer.join, _WATCHDOG_BOUND_SECONDS)

    assert monitor_done.is_set(), "control monitor escaped its safety bound"
    assert not failures
    assert begin_entered.is_set() and begin_acquired.is_set()
    assert callback_served.is_set()
    assert result.outcome is router.RouteOutcome.ACCEPTED
    assert any(item.startswith("begin-entered-thread-") for item in order)
    assert any(item.startswith("begin-acquired-thread-") for item in order)


@pytest.mark.asyncio
async def test_held_writer_does_not_starve_callback_before_release(
    isolated_db, monkeypatch
):
    """Callback service must precede foreign-writer release under contention."""
    from events import router
    from infra.config import DB_PATH

    owner = mk_session("router-writer-held")
    register_pr("o/r", 442, owner)
    begin_entered = threading.Event()
    begin_acquired = threading.Event()
    writer_ready = threading.Event()
    release_writer = threading.Event()
    holder_expired = threading.Event()
    callback_served = threading.Event()
    watchdog_done = threading.Event()
    callback_before_release: list[bool] = []
    order, record = _recording_list()
    failures: list[str] = []
    loop = asyncio.get_running_loop()
    _install_real_begin_observer(monkeypatch, begin_entered, begin_acquired, record)
    monkeypatch.setattr(router.delivery, "deliver_and_resolve_to_thread", _delivered)

    def callback() -> None:
        record("callback-served")
        callback_served.set()

    def watchdog() -> None:
        # Independent of asyncio: this safety bound releases the foreign writer
        # even if the event loop cannot run the callback it has just queued.
        if not begin_entered.wait(timeout=_WATCHDOG_BOUND_SECONDS):
            failures.append("real BEGIN was never entered")
        loop.call_soon_threadsafe(callback)
        callback_before_release.append(
            callback_served.wait(timeout=_WATCHDOG_BOUND_SECONDS)
        )
        record("watchdog-releasing-foreign-writer")
        release_writer.set()
        watchdog_done.set()

    holder = threading.Thread(
        target=_hold_real_writer,
        args=(str(DB_PATH), writer_ready, release_writer, holder_expired, record),
        daemon=True,
    )
    release_watchdog = threading.Thread(target=watchdog, daemon=True)
    holder.start()
    assert writer_ready.wait(timeout=_WATCHDOG_BOUND_SECONDS), "foreign writer did not acquire"
    release_watchdog.start()

    try:
        result = await router.route_event(
            "webhook.github.pr_opened", pr_payload("o/r", 442)
        )
        await asyncio.to_thread(release_watchdog.join, _WATCHDOG_BOUND_SECONDS)
    finally:
        release_writer.set()
        holder.join(timeout=_WATCHDOG_BOUND_SECONDS)
        release_watchdog.join(timeout=_WATCHDOG_BOUND_SECONDS)

    assert watchdog_done.is_set(), "watchdog escaped its safety bound"
    assert not holder_expired.is_set(), "holder failsafe expired before watchdog release"
    assert not failures
    assert begin_entered.is_set() and begin_acquired.is_set()
    assert callback_served.is_set(), "loop did not recover after foreign-writer release"
    assert result.outcome is router.RouteOutcome.ACCEPTED
    begin_entry = next(i for i, item in enumerate(order) if item.startswith("begin-entered-thread-"))
    begin_acquired_index = next(
        i for i, item in enumerate(order) if item.startswith("begin-acquired-thread-")
    )
    assert (
        order.index(next(item for item in order if item.startswith("foreign-held-thread-")))
        < begin_entry
        < order.index("watchdog-releasing-foreign-writer")
        < order.index(next(item for item in order if item.startswith("foreign-release-thread-")))
        < begin_acquired_index
    )
    # Callback service releases the holder while the real writer is still pending.
    assert callback_before_release == [True]
    assert order.index("callback-served") < order.index("watchdog-releasing-foreign-writer")
