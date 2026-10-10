"""Measure every GET route in-process: SQLite VM steps, statements and response bytes.

Run in a subprocess against one source tree (``--backend``) and one fixture
database (``--db``); the parent owns the environment (disposable workspace and
state roots, test mode, the one-worker-per-lane override). Only counts are
recorded as measurements. Wall time is logged in ``info`` for people to read and
is never an input to a verdict.

Counting: a progress handler every ``PROGRESS_EVERY`` VM instructions on every
SQLite connection the process opens (installed by wrapping ``sqlite3.connect``
before the application is imported), so a request that uses several
connections or threads is counted in full. A trace callback counts statements.
The SQLite build is the pinned one (``sqlite_pin``).

Two guards make a count mean one thing on every run, and a violated guard is an error with no verdict:
``Background`` (work the application starts that outlives the request is drained inside the measured
window, and anything it cannot trace to the measured request stops the run) and
``assert_one_worker_per_lane`` (the database lanes ran on a single pooled connection each).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from .sqlite_pin import PINNED_PACKAGE, sqlite3  # first: pins SQLite before anything below imports it

from . import clock, routes

PROGRESS_EVERY = 100
DRAIN_TIMEOUT_SECONDS = 120
#: The runner sets this for every harness run; ``backend/infra/db_async.py`` reads it (measurement-only).
WORKERS_VARIABLE = "CORSIA_DB_POOL_WORKERS"
#: Thread-name prefixes of the three database lanes (``<prefix>_<n>``).
LANE_PREFIXES = ("db", "db-read", "db-bulk")
_LANE_THREAD = re.compile(r"(db|db-read|db-bulk)_(\d+)")


class GuardError(RuntimeError):
    """A measurement guard was violated, so the counts cannot be trusted: no verdict, never a pass."""


class Counters:
    """Process-wide step and statement totals across every connection and thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.steps = 0
        self.statements = 0
        self.trace: list[str] = []  # "<thread>: <sql>" per statement, to trace a difference between runs
        self.threads: set[str] = set()  # every thread that ran a statement

    def on_progress(self) -> int:
        with self._lock:
            self.steps += PROGRESS_EVERY
        return 0

    def on_statement(self, sql: str) -> None:
        if not sql.startswith("--"):  # trigger bodies are reported as comments
            with self._lock:
                self.statements += 1
                name = threading.current_thread().name
                self.threads.add(name)
                self.trace.append(f"{'portal' if name.startswith('asyncio-portal') else name}: {' '.join(sql.split())[:90]}")

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.steps, self.statements


def install_counting(counters: Counters) -> None:
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_progress_handler(counters.on_progress, PROGRESS_EVERY)
        connection.set_trace_callback(counters.on_statement)
        return connection

    sqlite3.connect = counting_connect  # type: ignore[assignment]


class Background:
    """Application tasks that outlive the request that started them: recorded, drained, and held to account.

    The application starts work on its own event loop that a request does not wait for (the usage rollup
    refresh, the artifact pane's reconcile). Under a client that closes its loop with the response, such a
    task is cancelled wherever it stands, so whether its statements fall inside a measured window depends on
    thread timing. Here the loop outlives the request, every task the application's own code creates is
    recorded, and ``drain`` waits for the ones still running, so a read is measured together with the
    background work it starts, the same on every run. That work is real (a deployed instance runs it after
    the same read), which is why it is counted, not silenced.

    The guard fails closed, each case an error with no verdict:

    * a drain that does not finish in ``DRAIN_TIMEOUT_SECONDS``, or a drained task that raised;
    * an application task alive when a measured window opens (the window would count work that did not
      come from its request);
    * an application task created with no running task as its parent (a timer or another thread started it,
      so it cannot be traced to the request). Requests are sent one at a time and the previous request's
      tasks are drained before the next window opens, so a task created under a running task in the window
      belongs to the measured request or to a task it started.
    """

    def __init__(self, backend: Path) -> None:
        self._root = str(backend)
        self._live: list[tuple[str, asyncio.Task]] = []
        self._untraced: list[str] = []
        self.portal = None
        real_create_task = asyncio.BaseEventLoop.create_task
        background = self

        def create_task(loop, coro, **kwargs):
            task = real_create_task(loop, coro, **kwargs)
            code = getattr(coro, "cr_code", None)
            if code is not None and code.co_filename.startswith(background._root):
                qualified = getattr(code, "co_qualname", code.co_name)
                name = f"{Path(code.co_filename).relative_to(background._root).as_posix()}:{qualified}"
                if asyncio.current_task(loop) is None:
                    background._untraced.append(name)
                background._live.append((name, task))
            return task

        asyncio.BaseEventLoop.create_task = create_task  # type: ignore[method-assign]

    def attach(self, client) -> None:
        """Serve every request of ``client`` on one loop that stays alive between requests."""
        import anyio.from_thread

        self._manager = anyio.from_thread.start_blocking_portal()
        self.portal = client.portal = self._manager.__enter__()

    def detach(self) -> None:
        self._manager.__exit__(None, None, None)

    async def _wait(self) -> list[str]:
        drained: list[str] = []
        while True:
            pending = [(name, task) for name, task in self._live if not task.done()]
            if not pending:
                break
            drained += [name for name, _ in pending]
            _, still_running = await asyncio.wait([task for _, task in pending], timeout=DRAIN_TIMEOUT_SECONDS)
            if still_running:
                names = sorted(name for name, task in pending if task in still_running)
                raise GuardError(f"background task(s) did not finish in {DRAIN_TIMEOUT_SECONDS}s: {', '.join(names)}")
        for name, task in self._live:
            if not task.cancelled() and task.exception() is not None:
                raise GuardError(f"background task {name} raised {task.exception()!r}")
        self._live.clear()
        return sorted(drained)

    def drain(self) -> list[str]:
        """Wait for every application task still running; return their names (sorted, repeats kept)."""
        names = self.portal.call(self._wait)
        self.check_traceable()
        return names

    def check_traceable(self) -> None:
        if self._untraced:
            found = ", ".join(sorted(set(self._untraced)))
            raise GuardError(f"application task(s) started with no running task as parent, so not traceable to the "
                             f"measured request: {found}")

    def assert_quiet(self) -> None:
        """No application task is alive: the window about to open sees only the request's own work."""
        alive = sorted(name for name, task in self._live if not task.done())
        if alive:
            raise GuardError(f"application task(s) alive when a measured window opened: {', '.join(alive)}")


def assert_one_worker_per_lane(counters: Counters) -> None:
    """Black-box proof that every database lane served the whole run from one pooled thread.

    The runner sets ``CORSIA_DB_POOL_WORKERS=1``. This does not trust that: it reads the thread names that
    actually ran statements (and any lane thread still alive) and fails when a lane used a second thread, or
    when no lane thread ran at all. Which pooled connection serves a call moves its step count by a few
    samples, so a run on several threads per lane would not be exact.
    """
    names = counters.threads | {thread.name for thread in threading.enumerate()}
    used: dict[str, set[str]] = {prefix: set() for prefix in LANE_PREFIXES}
    for name in names:
        match = _LANE_THREAD.fullmatch(name)
        if match:
            used[match.group(1)].add(name)
    crowded = {prefix: sorted(found) for prefix, found in used.items() if len(found) > 1}
    if crowded:
        raise GuardError(f"a database lane used more than one worker thread ({crowded}); "
                         f"{WORKERS_VARIABLE} did not reach the application")
    if not any(used.values()):
        raise GuardError("no database lane thread ran a statement, so the one-worker-per-lane check proved nothing")


def _open_app(backend: Path, db_file: Path):
    """Import the target application with the fixture installed as its database."""
    sys.path[:0] = [str(backend), str(backend / "tests")]
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    from infra.database import DB_PATH, close_thread_db

    shutil.copyfile(db_file, DB_PATH)
    for suffix in ("-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    from auth_fixtures import OWNER_TOKEN, seed_test_accounts

    seed_test_accounts()
    close_thread_db()
    from fastapi.testclient import TestClient
    from main import app

    return app, TestClient(app, raise_server_exceptions=False), OWNER_TOKEN


def _extra_query(app) -> dict[str, str]:
    """Per-route query values that need the application (the registered metric ids)."""
    if importlib.util.find_spec("api.measurement_registry") is None:  # an older tree has no such route
        return {}
    from api.measurement_registry import REGISTRY
    return {"ids": ",".join(sorted(name for name, item in REGISTRY.items() if item.available))}


def measure_route(client, counters: Counters, background: Background, headers: dict, url: str) -> dict:
    # Warm: lazy imports and per-process caches settle, and a route that repairs state after its
    # response (the artifact pane's background reconcile) has made its one-time write before the
    # measured call, so that call sees the settled state on every run.
    for _ in range(2):
        client.get(url, headers=headers)
        background.drain()
    background.assert_quiet()
    steps0, statements0 = counters.snapshot()
    trace_from = len(counters.trace)
    started = time.monotonic()
    response = client.get(url, headers=headers)
    drained = background.drain()
    elapsed = time.monotonic() - started
    steps1, statements1 = counters.snapshot()
    trace = counters.trace[trace_from:]
    return {"steps": steps1 - steps0, "statements": statements1 - statements0,
            "bytes": len(response.content), "status": response.status_code,
            "info": {"seconds": round(elapsed, 4), "trace": trace, "background_tasks": drained,
                     "detail": response.text[:160] if response.status_code >= 400 else ""}}


def run(backend: Path, db_file: Path) -> dict:
    if os.environ.get(WORKERS_VARIABLE) != "1":
        raise GuardError(f"{WORKERS_VARIABLE}=1 is required: the runner sets it for every measurement")
    counters = Counters()
    install_counting(counters)
    app, client, owner = _open_app(backend, db_file)
    background = Background(backend)
    background.attach(client)
    ids = routes.fixture_ids(str(db_file))
    extra = _extra_query(app)
    headers = {"Authorization": f"Bearer {owner}"}
    measured: dict[str, dict] = {}
    not_called: dict[str, str] = {}
    for number, (template, route) in enumerate(routes.get_routes(app), 1):
        print(f"route {number}: {template}", flush=True)  # a stuck run shows which route it stopped on
        target = routes.build_target(template, route, ids, extra, owner)
        if target.url is None:
            not_called[template] = target.reason or "not callable"
            continue
        result = measure_route(client, counters, background, headers, target.url)
        result["url"] = target.url.replace(owner, "<owner credential>")  # a measurement file never holds the test credential
        measured[template] = result
        if result["status"] >= 400:
            not_called[template] = (f"HTTP {result['status']} from the fixture request "
                                    f"({result['info']['detail'][:100]}), so the read it guards is not exercised")
    assert_one_worker_per_lane(counters)
    return {"sqlite_version": sqlite3.sqlite_version, "sqlite_package": PINNED_PACKAGE,
            "progress_every": PROGRESS_EVERY, "routes": measured, "not_called": not_called,
            "clock": clock.FROZEN_NOW.isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    clock.install()  # before the application, so every module sees the frozen clock
    result = run(args.backend.resolve(), args.db.resolve())
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
