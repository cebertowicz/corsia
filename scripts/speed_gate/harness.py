"""Measure every GET route in-process: SQLite VM steps, statements and response bytes.

Run in a subprocess against one source tree (``--backend``) and one fixture
database (``--db``); the parent owns the environment (disposable workspace and
state roots, test mode). Only counts are recorded as measurements. Wall time is
logged in ``info`` for people to read and is never an input to a verdict.

Counting: a progress handler every ``PROGRESS_EVERY`` VM instructions on every
SQLite connection the process opens (installed by wrapping ``sqlite3.connect``
before the application is imported), so a request that uses several
connections or threads is counted in full. A trace callback counts statements.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import clock, routes

PROGRESS_EVERY = 100
#: One rollup step builds one day; the fixture spans well under this many.
SETTLE_STEP_LIMIT = 1000


class Counters:
    """Process-wide step and statement totals across every connection and thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.steps = 0
        self.statements = 0
        self.trace: list[str] = []  # "<thread>: <sql>" per statement, to trace a difference between runs

    def on_progress(self) -> int:
        with self._lock:
            self.steps += PROGRESS_EVERY
        return 0

    def on_statement(self, sql: str) -> None:
        if not sql.startswith("--"):  # trigger bodies are reported as comments
            with self._lock:
                self.statements += 1
                name = threading.current_thread().name
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


def _single_db_worker() -> None:
    """Serve every database job of each lane from one worker thread, so a measured call never depends on which thread ran it.

    The application runs each read on a pool of worker threads (the default pool and the read and bulk
    lanes), each with its own connection. A worker runs its connection setup statements the first time it
    serves a job, and its step counter carries a sampling phase from every statement it ran before. Which
    idle worker takes a job depends on thread timing, so the same call could cost one statement (or a few
    samples) more or less from run to run. The harness sends one request at a time, so one worker per lane
    serves it as the pool would, and the warm-up calls before each measurement settle that worker's connection.
    """
    if importlib.util.find_spec("infra.db_async") is None:
        return
    from infra import db_async
    for name in ("_pool", "_read_pool", "_bulk_pool"):  # an older tree has fewer lanes
        pool = getattr(db_async, name, None)
        if pool is not None and hasattr(pool, "_max_workers"):
            pool._max_workers = 1  # nothing has been submitted yet, so a second worker is never started


def _settle_usage_rollup() -> None:
    """Finish the usage rollup before any measurement, and stop requests from scheduling a refresh.

    A Measurement route starts a background task that builds the hourly rollup one day at a time while the
    request is served, so how much of it a measured request sees, and whether the task's own statements land
    inside the measured window, depends on thread timing. The harness measures a read on the settled state
    a running instance is in, and the refresh is background work, not part of any request. So the rollup is
    built here to its end on the frozen clock, and the scheduling call is replaced with a no-op.
    """
    if importlib.util.find_spec("api.usage_rollup") is None:  # an older tree has no rollup
        return
    from api import usage_rollup
    from infra.database import get_db

    usage_rollup.schedule_refresh = lambda *args, **kwargs: None  # type: ignore[assignment]
    db = get_db()
    try:
        for _ in range(SETTLE_STEP_LIMIT):
            if not usage_rollup.refresh_step(db):
                return
    finally:
        db.close()
    raise RuntimeError(f"the usage rollup was still building after {SETTLE_STEP_LIMIT} steps")


def _extra_query(app) -> dict[str, str]:
    """Per-route query values that need the application (the registered metric ids)."""
    if importlib.util.find_spec("api.measurement_registry") is None:  # an older tree has no such route
        return {}
    from api.measurement_registry import REGISTRY
    return {"ids": ",".join(sorted(name for name, item in REGISTRY.items() if item.available))}


def measure_route(client, counters: Counters, headers: dict, url: str) -> dict:
    # Warm: lazy imports and per-process caches settle, and a route that repairs state after its
    # response (the artifact pane's background reconcile) has made its one-time write before the
    # measured call, so that call sees the settled state on every run.
    client.get(url, headers=headers)
    client.get(url, headers=headers)
    steps0, statements0 = counters.snapshot()
    trace_from = len(counters.trace)
    started = time.monotonic()
    response = client.get(url, headers=headers)
    elapsed = time.monotonic() - started
    steps1, statements1 = counters.snapshot()
    trace = counters.trace[trace_from:]
    return {"steps": steps1 - steps0, "statements": statements1 - statements0,
            "bytes": len(response.content), "status": response.status_code,
            "info": {"seconds": round(elapsed, 4), "trace": trace,
                     "detail": response.text[:160] if response.status_code >= 400 else ""}}


def run(backend: Path, db_file: Path) -> dict:
    counters = Counters()
    install_counting(counters)
    app, client, token = _open_app(backend, db_file)
    _single_db_worker()
    _settle_usage_rollup()
    ids = routes.fixture_ids(str(db_file))
    extra = _extra_query(app)
    headers = {"Authorization": f"Bearer {token}"}
    measured: dict[str, dict] = {}
    not_called: dict[str, str] = {}
    for number, (template, route) in enumerate(routes.get_routes(app), 1):
        print(f"route {number}: {template}", flush=True)  # a stuck run shows which route it stopped on
        target = routes.build_target(template, route, ids, extra, token)
        if target.url is None:
            not_called[template] = target.reason or "not callable"
            continue
        result = measure_route(client, counters, headers, target.url)
        result["url"] = target.url.replace(token, "<owner token>")  # a measurement file never holds the test credential
        measured[template] = result
        if result["status"] >= 400:
            not_called[template] = (f"HTTP {result['status']} from the fixture request "
                                    f"({result['info']['detail'][:100]}), so the read it guards is not exercised")
    return {"sqlite_version": sqlite3.sqlite_version, "progress_every": PROGRESS_EVERY,
            "routes": measured, "not_called": not_called,
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
