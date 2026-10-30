
from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import gc
import os
import signal
import sqlite3
import stat
import sys
import textwrap
import threading

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


_TEST_MODEL_WINDOWS = [
    ("anthropic/claude-3-5-sonnet", 200000),
    ("anthropic/claude-sonnet-4-6", 200000),
    ("claude-code/opus", 1_000_000),
    ("claude-code/sonnet", 200000),
    ("claude-code/tiny", 128),
    ("codex/gpt-5-codex", 200000),
    ("omlx@local/qwen", 200000),
    ("oMLX/qwen", 200000),
    ("openai/test-checkpoint", 200000),
    ("opus", 200000),
]

# Bounded rounds for the terminal-driver reap; one round per possible
# successor generation plus one to prove stability.
_TERMINAL_REAP_ROUNDS = 3


@pytest.fixture
async def _cc_runtime_cleanup():
    """Terminate CC handles while the test event loop is still alive."""
    yield

    from subagents.cc_runtime_state import _LIVE

    try:
        for thread_id in _LIVE.ids():
            live = _LIVE.pop(thread_id, None)
            session = getattr(live, "session", None)
            terminate = getattr(session, "terminate", None)
            if terminate is not None:
                await asyncio.wait_for(terminate(), timeout=2.0)
            # The session's fire-and-forget settlement driver keeps executing
            # the woken parent's turn after the test ends; left unowned it
            # lands in the runner-close cancel window and can strand teardown
            # (#2261).
            await _join_terminal_driver(session)
    finally:
        # Reset the registry and the DB pool unconditionally: a failing
        # terminate/reap must fail its own test, never cascade live runtimes
        # into the next one.
        _LIVE.reset()

        import infra.database as database
        _reset_db_pool(database)


async def _join_terminal_driver(session, timeout: float = 2.0) -> None:
    """Cancel and bounded-join a session's terminal driver and event task.

    The driver is deliberately unobserved in production — settlement must
    outlive the session. But once the test loop is the only owner left, it
    must be cancelled and reaped before the loop is torn down. A driver that
    survives the bound fails the test by name instead of stranding teardown
    back into the runner-close cancel window (#2261).

    Only real CCStreamSession instances carry the terminal-driver contract;
    test doubles (None, SimpleNamespace, custom fakes) are skipped without
    an attribute check, and a rename on the real class raises instead of
    silently no-oping.

    Completing the terminal event replaces ``_terminal_driver`` with a
    successor (``observe_terminal_event`` → ``retain_terminal_driver``),
    and that replacement runs from a ``call_soon``-scheduled done callback.
    ``terminate()`` settles the event inside its own awaits, so the callback
    can still be queued when this reap first reads both attributes — a
    single pass then leaves the successor unowned, and its cancellation
    defers through the durable parent drive (``drive_after``/``run_in_db``),
    parking ``Runner._cancel_all_tasks`` forever (#2261). The reap therefore
    drains the callback queue and repeats until a drained loop turn yields
    no new driver or event task.
    """
    from subagents.cc_stream import CCStreamSession

    if not isinstance(session, CCStreamSession):
        return
    for name in ("_terminal_driver", "_terminal_event"):
        if not hasattr(session, name):
            # A rename makes the reap a silent no-op; fail loudly instead so
            # the fixture is kept in step with the session object.
            raise AssertionError(
                f"session {session!r} no longer exposes {name}; "
                "the terminal-driver reap in _cc_runtime_cleanup needs updating"
            )
    for round_no in range(1, _TERMINAL_REAP_ROUNDS + 1):
        for name in ("_terminal_driver", "_terminal_event"):
            task = getattr(session, name)
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            if not task.done():
                pytest.fail(
                    f"{name} survived cancel and the {timeout}s join bound: {task!r}"
                )
        # Run done callbacks queued while the joins above (or terminate()
        # before them) yielded the loop, then re-read: a replaced driver or
        # a new terminal event means the session still installs successors.
        for _ in range(2):
            await asyncio.sleep(0)
        replaced = [
            name
            for name in ("_terminal_driver", "_terminal_event")
            if (task := getattr(session, name)) is not None and not task.done()
        ]
        if not replaced:
            return
        if round_no == _TERMINAL_REAP_ROUNDS:
            pytest.fail(
                "terminal driver/event kept being replaced after "
                f"{_TERMINAL_REAP_ROUNDS} reap rounds: {replaced}"
            )


def _reset_db_pool(database, *, reconfigure=None) -> None:
    """Close the old pool and install test configuration before admitting work."""
    database.shutdown_pool(reconfigure)


def _seed_model_profiles(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO model_profiles (model_id, context_window) "
        "VALUES (?, ?)",
        _TEST_MODEL_WINDOWS,
    )



def _seed_test_model_catalog(conn: sqlite3.Connection) -> None:
    rows = []
    for model_id, context_window in _TEST_MODEL_WINDOWS:
        if model_id.startswith("claude-code/"):
            harness, provider, backend = "claude-code", "claude-code", "cc"
            selector = model_id.split("/", 1)[1]
            reported = {
                "opus": "claude-opus-4-8",
                "sonnet": "claude-sonnet-4-8",
            }.get(selector, selector)
            metadata = '{"resolved_identity":"%s"}' % reported
        elif model_id.startswith("codex/"):
            harness, provider, backend = "codex", "codex", "codex"
            selector = model_id.split("/", 1)[1]
            metadata = '{"resolved_identity":"%s"}' % selector
        else:
            harness, backend, metadata = "native", "api", "{}"
            provider = model_id.split("/", 1)[0].lower() if "/" in model_id else "test"
            selector = model_id.split("/", 1)[-1]
        rows.append((
            model_id, harness, provider, selector, model_id, model_id, backend,
            "test", 1, 1, context_window, metadata, "t", "t",
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,context_window,metadata_json,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "UPDATE models SET default_max_tokens=32768 "
        "WHERE id='openai/test-checkpoint'"
    )
    conn.executemany(
        "INSERT OR REPLACE INTO connections "
        "(id,type,provider,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        [
            ("test-cc", "cli", "claude-code", "available", "t", "t"),
            ("test-codex", "cli", "codex", "available", "t", "t"),
        ],
    )


@pytest.fixture
def measurement_clock(monkeypatch):
    """Pin the Measurement page's today to Tuesday 2026-09-29, so seeded September data sits in
    known periods: the last completed week is 2026-09-21 and the last completed day 2026-09-28."""
    from datetime import datetime, timezone

    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 9, 29, 12, tzinfo=timezone.utc))


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    import infra.database as db_mod
    from auth_fixtures import seed_test_accounts

    db_path = tmp_path / "test.db"

    def reconfigure() -> None:
        monkeypatch.setattr("infra.config.DB_PATH", db_path)
        monkeypatch.setattr("infra.database.DB_PATH", db_path)
        # Unattributed-call counts wait in memory for their Turn's terminal flush,
        # keyed by Turn id. Tests reuse ids like "turn-1", so a count left by one
        # test would otherwise flush into the next test's fresh database.
        monkeypatch.setattr("plans.tool_dispatches._UNATTRIBUTED_COUNTS", {})

        from infra.schema import SCHEMA_SQL
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Match what startup does: WAL is a persistent, database-level property, so
        # establishing it here means a pooled connection opened later never has to
        # convert the file. Converting takes an exclusive lock that no busy timeout
        # waits out, so a worker opening its first connection during another
        # thread's write would fail instantly with "database is locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        from events.observe_rules import sync_shipped_rules
        sync_shipped_rules(conn)
        _seed_model_profiles(conn)
        _seed_test_model_catalog(conn)
        seed_test_accounts(conn)
        conn.commit()
        conn.close()

    _reset_db_pool(db_mod, reconfigure=reconfigure)

    yield db_path

    _reset_db_pool(db_mod)


class _NoClose:

    def __init__(self, c):
        self._c = c

    def execute(self, *a):
        return self._c.execute(*a)

    def commit(self):
        self._c.commit()

    @property
    def in_transaction(self):
        return self._c.in_transaction

    def close(self):
        pass


@pytest.fixture
def main_db(tmp_path, monkeypatch, _cc_runtime_cleanup):
    from auth_fixtures import seed_test_accounts
    from infra.schema import SCHEMA_SQL

    db_path = tmp_path / "m.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # See the note in the isolated_db fixture: WAL here, so a pooled connection
    # opened during another thread's write never converts the file.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    _seed_model_profiles(conn)
    _seed_test_model_catalog(conn)
    seed_test_accounts(conn)
    conn.execute(
        "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
        "VALUES ('main-1', 'steve', 'claude-code/opus', 't', 't')"
    )
    conn.commit()
    wrapper = _NoClose(conn)

    from infra import config, database

    def reconfigure() -> None:
        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(database, "DB_PATH", db_path)

    _reset_db_pool(database, reconfigure=reconfigure)

    yield wrapper
    conn.close()


_FAKE = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def visible_content(content):
        marker = "User message:\\n"
        if marker in content:
            return content.split(marker, 1)[1]
        return content
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        c = visible_content(msg.get("message", {{}}).get("content", ""))
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + c}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"echo:" + c,"total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


@pytest.fixture
def fake_claude(tmp_path):
    p = tmp_path / "fake_claude"
    p.write_text(_FAKE.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


async def _wait(pred, tries: int = 400, delay: float = 0.02) -> bool:
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


@pytest.fixture(autouse=True)
def _omlx_leftover_probe_never_runs_a_binary(monkeypatch):
    """Tests fake `shutil.which("omlx")` paths; none of them may be executed to probe."""
    from llm import local_setup_executable

    monkeypatch.setattr(local_setup_executable, "_is_broken_leftover", lambda _path: False)


@pytest.fixture(autouse=True)
def _isolated_default_claude_root(monkeypatch, tmp_path_factory):
    """Each test's Default Claude root is its own, never the developer's ~/.claude."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("default-claude")))


@pytest.fixture(autouse=True)
def _no_held_usage_between_tests():
    """Usage held through a lock window is process-wide; each test starts with none."""
    held = sys.modules.get("infra.retained_usage")
    if held is not None:
        held._held.clear()
    yield


@pytest.fixture(autouse=True, scope="session")
def _imported_bindings_bound_before_any_patch():
    """Load modules that bind patched dependencies by name before test fixtures run.

    Tests replace these source-module attributes. A lazy first import inside a
    replacement would retain that fake for the rest of the worker after the patch
    restores, so every such consumer is bound here once against the normal getter.
    """
    import infra.checkpoint_install_preflight  # noqa: F401
    import infra.checkpoint_queries  # noqa: F401
    import llm.providers.omlx_lifecycle  # noqa: F401
    import llm.providers.omlx_settings  # noqa: F401
    yield



@pytest.fixture(autouse=True)
def _no_local_model_between_tests(monkeypatch, tmp_path_factory):
    """The set-up model record and Welcome's pick outlive a test; each test starts with neither."""
    from llm import local_model

    record = tmp_path_factory.mktemp("local-model") / "local-model.json"
    monkeypatch.setattr(local_model, "_record_path", lambda: record)
    prepare = sys.modules.get("llm.local_setup_prepare")
    if prepare is not None:
        monkeypatch.setattr(prepare, "_prepare", prepare._Prepare())



@pytest.fixture(autouse=True)
def _exact_tail_carry_pin(request, monkeypatch):
    """Pin the exact Assistant Step tail window to zero for marked modules.

    Modules marked ``pin_raw_tail_off`` cover boundary-independent durable
    mechanics (CAS, recovery, staging, commit, FIFO) at sealed-head landings.
    Exact-tail derivation semantics have their own targeted coverage.
    """
    if request.node.get_closest_marker("pin_raw_tail_off") is not None:
        from core import checkpoint_boundary, checkpoint_generation

        monkeypatch.setattr(
            checkpoint_boundary,
            "CHECKPOINT_EXACT_TAIL_MAX_TOKENS",
            0,
        )
        monkeypatch.setattr(
            checkpoint_generation,
            "CHECKPOINT_EXACT_TAIL_MAX_TOKENS",
            0,
        )


@pytest.fixture(autouse=True)
def _no_public_model_catalog(monkeypatch):
    """External discovery reads models.dev; no test reaches the internet for it."""
    monkeypatch.setenv("CORSIA_MODEL_CATALOG_URL", "")


@pytest.fixture(autouse=True)
def _no_real_harness_cli(request, monkeypatch):
    """No unit test runs a real Claude Code or Codex CLI for discovery (#2792).

    The model list, thread list, thread create and startup all start catalog
    or usage checks, and on a host with the CLIs installed they would spawn
    them. Tests that exercise discovery replace these leaves themselves; a
    test of a leaf over a faked process marks ``real_harness_cli``.
    """
    if request.node.get_closest_marker("real_harness_cli") is not None:
        return
    from connections import harness_models, harness_usage_check

    async def _blocked(*_args, **_kwargs):
        raise RuntimeError("unit tests never run a real harness CLI (#2792)")

    monkeypatch.setattr(harness_models, "binary_version", _blocked)
    for provider, (_discover, normalize, source) in list(harness_models._ADAPTERS.items()):
        monkeypatch.setitem(harness_models._ADAPTERS, provider, (_blocked, normalize, source))
    monkeypatch.setattr(harness_usage_check, "_binary_for", _blocked)

    async def _no_binary(_provider):
        return None

    # Account sign-in, status and logout find no vendor binary, so they start nothing.
    monkeypatch.setattr("connections.account_controls._binary", _no_binary)


def _on_path(name: str, search: str) -> str | None:
    # ``lexists``, not ``which``: a sandboxed run may see an install it cannot stat.
    for directory in search.split(os.pathsep):
        candidate = os.path.join(directory or os.curdir, name)
        if os.path.lexists(candidate):
            return candidate
    return None


@functools.cache
def _installed_harness_clis() -> frozenset[str]:
    """Real paths of the Claude Code and Codex CLIs installed on this host."""
    from cli_location import service_path

    found = set()
    for search in (os.environ.get("PATH", os.defpath), service_path()):
        for name in ("claude", "codex"):
            if (candidate := _on_path(name, search)) is not None:
                found.add(os.path.realpath(candidate))
    return frozenset(found)


def _spawned_path(program, env) -> str | None:
    program = os.fspath(program)
    if os.sep not in program:
        search = (os.environ if env is None else env).get("PATH", os.defpath)
        program = _on_path(program, search)
    return None if program is None else os.path.realpath(program)


@pytest.fixture(autouse=True)
def _no_installed_harness_spawn(request, monkeypatch):
    """No unit test starts the installed Claude Code or Codex CLI (#732).

    One started from a unit test outlives it: cancelled at loop close, its
    reap can wait on pipes a descendant still holds. Starting either fails as
    if it were not installed; fake CLIs under tmp paths still run. Integration
    and cloud tests that select the stock CLI on purpose are exempt.
    """
    if any(request.node.get_closest_marker(name) is not None
           for name in ("integration", "cloud_required")):
        return
    installed = _installed_harness_clis()
    spawn = asyncio.create_subprocess_exec

    async def _guarded(program, *args, **kwargs):
        if _spawned_path(program, kwargs.get("env")) in installed:
            raise FileNotFoundError(
                errno.ENOENT, "unit tests never start the installed harness CLI (#732)", program,
            )
        return await spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _guarded)


# Well above the longest bounded teardown wait (the browser process fixture
# waits EXIT_TIMEOUT, 30 s, per daemon before killing it) and well inside the
# 15-minute job limit: a teardown, event-loop close included, that outlives it
# is stuck, not slow (#732).
_TEARDOWN_LIMIT_SECONDS = 180.0


def _report_stuck_teardown(item) -> None:
    """Name the stuck test and its pending tasks, then interrupt the run."""
    capture = item.config.pluginmanager.getplugin("capturemanager")
    try:
        # Teardown output is captured; the report must reach the real stderr.
        with capture.global_and_fixture_disabled() if capture else contextlib.nullcontext():
            print(f"\nteardown of {item.nodeid} still running after "
                  f"{_TEARDOWN_LIMIT_SECONDS:g}s (#732); pending asyncio tasks:", file=sys.stderr)
            for task in [obj for obj in gc.get_objects()
                         if isinstance(obj, asyncio.Task) and not obj.done()]:
                print(f"--- {task!r}", file=sys.stderr)
                task.print_stack(file=sys.stderr)
            sys.stderr.flush()
    finally:
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)


class _TeardownWatchdog:
    """One thread for the whole session, started before any test runs.

    Starting a thread per teardown would run while the test's own patches are
    still in place; one that refuses ``Thread.start`` would then skip that
    teardown, leave its patches behind and fail every later test.
    """

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._item = None
        threading.Thread(target=self._watch, name="teardown-watchdog", daemon=True).start()

    def arm(self, item) -> None:
        with self._changed:
            self._item = item
            self._changed.notify()

    def disarm(self) -> None:
        with self._changed:
            self._item = None
            self._changed.notify()

    def _watch(self) -> None:
        with self._changed:
            while True:
                while self._item is None:
                    self._changed.wait()
                item = self._item
                if not self._changed.wait(_TEARDOWN_LIMIT_SECONDS) and self._item is item:
                    break
        _report_stuck_teardown(item)


_teardown_watchdog: _TeardownWatchdog | None = None


def pytest_sessionstart(session):
    global _teardown_watchdog
    _teardown_watchdog = _TeardownWatchdog()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    if _teardown_watchdog is None:
        return (yield)
    _teardown_watchdog.arm(item)
    try:
        return (yield)
    finally:
        _teardown_watchdog.disarm()


@pytest.fixture(autouse=True)
def _system_projection_readable(monkeypatch):
    """Tests project System files into disposable tmp workspaces, which are not
    the configured workspace and so are never ordinary agent read scope. The
    gate itself is tested in test_system_projection with the real check."""
    monkeypatch.setattr(
        "core.system_projection._agent_readable", lambda _projection: True,
    )


@pytest.fixture(autouse=True)
def _github_read_budget_starts_empty():
    """The GitHub read budget is process-wide; each test starts with none learned."""
    budget = sys.modules.get("events.connectors.github_read_budget")
    if budget is not None:
        budget.budget.clear()
    yield
