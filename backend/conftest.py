"""Backend-wide pytest fixtures."""

from __future__ import annotations

import atexit
import importlib
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from typing import Any

import pytest

_BACKEND_DIR = os.path.dirname(__file__)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# Must run before any test imports ``infra.config`` because that module resolves
# paths at import time. Tests default to separate ephemeral workspace and state
# roots unless the caller explicitly provides the corresponding override.
_ephemeral_roots: list[str] = []
if not os.environ.get("CORSIA_WORKSPACE"):
    _ephemeral_ws = tempfile.mkdtemp(prefix="corsia-test-ws-")
    _ephemeral_roots.append(_ephemeral_ws)
    os.environ["CORSIA_WORKSPACE"] = _ephemeral_ws

if not os.environ.get("CORSIA_STATE_DIR"):
    _ephemeral_state = tempfile.mkdtemp(prefix="corsia-test-state-")
    _ephemeral_roots.append(_ephemeral_state)
    os.environ["CORSIA_STATE_DIR"] = _ephemeral_state


def _cleanup_ephemeral_roots(paths: tuple[str, ...]) -> None:
    """Remove conftest-minted workspace and state roots at exit."""
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_ephemeral_roots, tuple(_ephemeral_roots))
os.environ.setdefault("CORSIA_TEST_MODE", "1")
os.environ.setdefault("CORSIA_ANALYTICS_DISABLE", "1")

# Every SQLite connection a test opens refuses an aggregate-input write to
# ``plan_nodes`` outside the sanctioned writers (#731); installed before any
# connection exists and after the roots above, which importing ``plans`` reads.
from tests import plan_writer_authorizer  # noqa: E402

plan_writer_authorizer.install()


class _CapturedEmits(list):
    """List subclass with an extra ``by_event`` helper."""

    def by_event(self, event_name: str) -> list[dict[str, Any]]:
        """Return all property dicts captured for ``event_name``."""
        return [props for evt, _sid, props in self._entries if evt == event_name]

    @property
    def _entries(self) -> list[tuple[str, str | None, dict[str, Any]]]:
        return getattr(self, "_full", [])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Compare every plan a test wrote with the recount oracle, while its fixtures are alive."""
    result = yield
    mismatches = plan_writer_authorizer.recount_changed_plans()
    if mismatches:
        pytest.fail(
            "stored plan aggregates differ from the recount after this test: "
            + ", ".join(mismatches[:10])
            + " (a raw plan_nodes write needs `raw_plan_seed`; see tests/plan_writer_authorizer.py)",
            pytrace=False,
        )
    return result


@pytest.fixture(scope="session")
def _browser_sweep_test_root(tmp_path_factory):
    """Keep startup recovery away from every real browser socket directory."""
    return tmp_path_factory.mktemp("browser-sweep")


@pytest.fixture(autouse=True)
def _isolate_browser_startup_sweep(monkeypatch, _browser_sweep_test_root):
    from functools import partial
    from host_access import browser_orphan

    monkeypatch.setattr(browser_orphan, "close_dead_sessions",
                        partial(browser_orphan.close_dead_sessions, root=_browser_sweep_test_root))


@pytest.fixture
def local_model_host(monkeypatch):
    """Pin the local-model host gate, including its imported alias."""
    from llm import local_setup_gates, local_sources

    def set_host(*, is_mac):
        monkeypatch.setattr(local_setup_gates, "host_never_runs_local", lambda: not is_mac)
        monkeypatch.setattr(local_sources, "host_never_runs_local", lambda: not is_mac)

    return set_host


@pytest.fixture
def mac_local_model_host(local_model_host):
    local_model_host(is_mac=True)


@pytest.fixture
def mock_emit(monkeypatch):
    """Replace ``emit_product_event`` with a list-capturing stub."""
    from infra import product_events

    captured = _CapturedEmits()
    full: list[tuple[str, str | None, dict[str, Any]]] = []
    captured._full = full  # type: ignore[attr-defined]
    captured.full = full  # type: ignore[attr-defined]

    def fake_emit(
        category: str,
        action: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        event = f"{category}.{action}"
        captured.append((event, thread_id))
        full.append((event, thread_id, dict(properties or {})))

    monkeypatch.setattr(product_events, "emit_product_event", fake_emit)

    # Patch names rebound by ``from infra.product_events import emit_product_event``.
    for mod_name in (
        "api.threads",
        "api.thread_mutations",
        "ws.turn",
        "api.network_servers",
        "api.providers",
        "api.uploads",
        "core.turns_util",
        "main",
    ):
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "emit_product_event"):
            monkeypatch.setattr(mod, "emit_product_event", fake_emit)

    return captured


@pytest.fixture
def mock_posthog(mock_emit):
    """Deprecated alias for ``mock_emit``."""
    return mock_emit


@pytest.fixture(scope="session", autouse=True)
def _init_isolated_db_schema():
    """Initialize the ephemeral workspace's DB schema once per session."""
    import shutil

    import connections
    from infra.database import init_db

    # init_db seeds the CLI connection rows from what this host has installed
    # and signed in, so a test reaching the Codex connection gate would pass or
    # fail with the host's login state (#2975). The session database instead
    # gets the same deterministic available rows the per-test databases seed.
    with pytest.MonkeyPatch.context() as seed_probe:
        seed_probe.setattr(shutil, "which", lambda name, *_a, **_k: name)
        seed_probe.setattr(connections, "_gh_status", lambda _binary: (True, None))
        seed_probe.setattr(connections, "_codex_sign_in_state", lambda _binary: "ready")
        init_db()
    # A token authenticates only if it is a row, so the session database needs
    # the accounts the suite signs in as before any authenticated route is
    # called. ``tests/auth_fixtures.py`` owns which those are.
    from auth_fixtures import seed_test_accounts
    seed_test_accounts()
    yield


@pytest.fixture(autouse=True)
def _no_host_harness_sign_in_check(monkeypatch):
    """Startup's sign-in check never runs a host harness CLI in a unit test.

    Its result would depend on whichever codex or claude the host has and is
    signed in to. Tests of the check itself call ``connections.source_state``
    directly with their own stubs. GitHub's ``gh auth status``, which the
    seed and the model-source recheck run, reads signed in unless a test
    patches it.
    """
    async def _skip(_provider, *, rediscover=False):
        return None

    monkeypatch.setattr("connections.startup.check_harness_source", _skip)
    monkeypatch.setattr("connections._gh_status", lambda _binary: (True, None))


@pytest.fixture(autouse=True)
def _hide_session_app_loop(request, monkeypatch):
    """Only a test that serves the integration app may reach its application loop.

    The session-scoped integration app keeps its loop captured after the test
    that started it, so a later unit test's after-commit nudge would be handed
    to that loop and arrive on another thread, at a time the test cannot
    order. Unit tests that assert deliveries patch ``app_loop.submit`` themselves.
    """
    if "integration_client" not in request.fixturenames:
        import infra.app_loop as app_loop

        monkeypatch.setattr(app_loop, "_LOOP", None)


@pytest.fixture(autouse=True)
def _application_loop_per_test(request, monkeypatch):
    """A test sees the integration app's loop only when it uses that app.

    The session's integration app captures its loop once, in a module global.
    Left alone, whether a test finds a live loop depends on which tests ran
    before it: one that calls ``app_loop.clear`` removes it for every later
    test, and a unit test that runs after the app started hands its
    after-commit broadcasts to a loop it does not own.
    """
    import infra.app_loop as app_loop

    loop = None
    if "integration_client" in request.fixturenames:
        loop = request.getfixturevalue("integration_client").application_loop
    monkeypatch.setattr(app_loop, "_LOOP", loop)


@pytest.fixture(autouse=True)
def _clear_idle_checkpoint_timers():
    """Never let one test's armed idle-checkpoint timer outlive it.

    A timer armed under a test's own event loop becomes unreachable once
    that loop closes; cancel_all's foreign-loop guard then drops it from the
    registry silently rather than crash a later loop. Clearing here, for
    every test, is what keeps that drop from ever mattering instead of
    relying on each idle-checkpoint test file to remember its own cleanup.
    """
    import infra.idle_checkpoint_timers as idle_checkpoint_timers

    yield
    idle_checkpoint_timers.cancel_all()


@pytest.fixture(scope="session")
def integration_workspace():
    """Return the session's isolated workspace, which the integration app serves.

    The top of this file already gives the session its own workspace before
    ``infra.config`` is imported, and ``infra.config`` refuses the production
    workspace. Pointing the integration app somewhere else would mean reloading
    ``infra.config`` mid-session, and every module that imported ``WORKSPACE``
    by value (the sandbox's allowed roots among them) would keep the old root:
    each later test in the process would resolve paths against one workspace
    and authorize them against another (#2949).
    """
    from infra import config

    os.environ["CORSIA_ANALYTICS_DISABLE"] = "1"
    return config.WORKSPACE


@pytest.fixture(scope="session")
def integration_client(integration_workspace):
    """Provide a FastAPI TestClient wrapping the real CORSIA app.

    Session-scoped so all integration tests share one in-process app.
    """
    import asyncio
    import app_lifecycle
    import app_startup_runtime
    import api.usage_rollup as usage_rollup
    import automation.plan_liveness as plan_liveness
    import automation.restart_startup as restart_startup
    import connections
    import llm.network_discovery as network_discovery
    import llm.live_cost_map as live_cost_map
    import ws.presence_control as presence_control
    import infra.bulletin_live as bulletin_live
    from main import app
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    async def _skip_startup_network_refreshes() -> list[dict]:
        return []

    async def _suspend_forever(*_args) -> None:
        await asyncio.Event().wait()

    # Every long-running loop production starts at app startup. Each one
    # outlives this session-scoped app and keeps polling whichever isolated
    # per-test database happens to be active later, which has segfaulted the
    # suite from multiple loops (plan liveness, nightly, delivery recovery,
    # idle retirement). Startup recovery can also outlive startup and reach a
    # later test's database. Suspend these here; dedicated lifecycle tests
    # exercise the real owners.
    _BACKGROUND_LOOPS = (
        (plan_liveness, "plan_liveness_sweep_loop"),
        (restart_startup, "run_startup_drive"),
        (network_discovery, "health_sweep_loop"),
        (app_startup_runtime, "nightly_job_loop"),
        (app_startup_runtime, "idle_retirement_loop"),
        (presence_control, "presence_sweep_loop"),
        (bulletin_live, "live_bulletin_loop"),
    )

    # The integration app shares this pytest process with tests that temporarily
    # point infra.database at per-test files. Long-running production startup
    # work must not later read or rewrite whichever isolated database happens to
    # be active then. Dedicated lifecycle tests exercise the real owners.
    client = TestClient(app)
    with ExitStack() as suspended:
        suspended.enter_context(patch.object(
            app_lifecycle,
            "_startup_harness_discovery",
            _skip_startup_network_refreshes,
        ))
        # A cloud discovery waiting on the startup map can otherwise resume
        # after a per-test database reset has begun.
        suspended.enter_context(patch.object(
            app_lifecycle, "_startup_cloud_discovery", _skip_startup_network_refreshes,
        ))
        # Startup's init_db would re-seed the session database's CLI rows from
        # whatever this host has installed, undoing the deterministic rows
        # ``_init_isolated_db_schema`` seeded (#2975).
        suspended.enter_context(patch.object(
            connections, "seed_builtin_connections", lambda **_kwargs: 0,
        ))
        # The session app cannot refresh mutable rollup caches while per-test
        # fixtures repeatedly replace the active isolated database.
        suspended.enter_context(patch.object(usage_rollup, "schedule_refresh", lambda: None))
        suspended.enter_context(patch.object(live_cost_map, "_refresh", _skip_startup_network_refreshes))
        for module, loop_name in _BACKGROUND_LOOPS:
            suspended.enter_context(
                patch.object(module, loop_name, _suspend_forever)
            )
        client.__enter__()
    import infra.app_loop as app_loop

    # The loop startup captured; ``_application_loop_per_test`` hands it to each test that uses this app.
    client.application_loop = app_loop._LOOP
    try:
        yield client
    finally:
        client.__exit__(None, None, None)
