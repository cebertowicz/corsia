"""Every model source reports one state and one fix (#1910, #1912, OB3)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import ANY
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN
from vendor_account_fixtures import default_id, default_receipt

_KEYS = {"source", "name", "state", "prior_state", "reason", "fix", "checked_at"}


def _put(provider, status, **config):
    from connections.models import ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider=provider,
                      config={"binary": f"/bin/{provider}", **config}, status=status)


async def _no_check(_provider):
    return None


@pytest.fixture
def client(isolated_db, monkeypatch):
    from api.provider_sources import router
    from infra.database import write_transaction

    # A recheck never signs in to a harness on the host; tests of that name their own.
    monkeypatch.setattr("api.provider_sources.check_harness_source", _no_check)

    # The seeded test catalog owns the selectors a not-ready harness falls back to.
    with write_transaction(None, scope="test_clear_harness_rows") as db:
        db.execute("DELETE FROM models WHERE harness IN ('claude-code', 'codex')")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, headers={"Authorization": f"Bearer {OWNER_TOKEN}"})


def _sources(client):
    body = client.get("/api/providers/sources").json()
    return {s["source"]: s for s in body["sources"]}



class TestSeedCodex:
    def _seed(self, monkeypatch):
        import connections

        monkeypatch.setattr("shutil.which", lambda name, *a, **k: f"/bin/{name}")
        monkeypatch.setattr(connections, "_gh_status", lambda _binary: (True, None))

        connections.seed_builtin_connections()

    def test_seed_checks_installation_without_probing_the_ambient_login(self, isolated_db, client, monkeypatch):
        import connections

        monkeypatch.setattr("shutil.which", lambda name, *a, **k: f"/bin/{name}")
        with monkeypatch.context() as probe:
            probe.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("ambient probe"))
            self._seed(probe)
            connections.seed_builtin_connections()
        assert _sources(client)["codex"]["state"] == "needs_auth"
        monkeypatch.setattr("shutil.which", lambda name, *a, **k: None if name == "codex" else "/bin/x")
        monkeypatch.setattr("os.path.isfile", lambda p: False)
        connections.seed_builtin_connections()
        assert _sources(client)["codex"]["state"] == "not_installed"

    def test_seeded_claude_is_not_ready_until_its_account_is_checked(self, isolated_db, client, monkeypatch):
        self._seed(monkeypatch)
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"]) == ("needs_auth", "Not signed in")
        _signed_in_unchecked("claude-code")
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"], claude["checked_at"]) == ("unverified", "Not checked yet", None)

    def test_unverified_cli_reports_a_proven_package_manager(self, isolated_db, client, monkeypatch):
        import cli_location

        self._seed(monkeypatch)
        _signed_in_unchecked("claude-code")
        monkeypatch.setattr(cli_location, "package_manager_for", lambda provider: "homebrew" if provider == "claude-code" else None)

        assert _sources(client)["claude-code"]["cli_package_manager"] == "homebrew"

    def test_a_missing_binary_reads_not_installed_with_a_signed_in_account(self, isolated_db, client, monkeypatch):
        import connections
        from vendor_account_fixtures import signed_in_default

        self._seed(monkeypatch)
        signed_in_default("claude-code")
        assert _sources(client)["claude-code"]["state"] == "ready"
        monkeypatch.setattr("shutil.which", lambda name, *a, **k: None if name == "claude" else f"/bin/{name}")
        monkeypatch.setattr("os.path.isfile", lambda p: False)
        connections.seed_builtin_connections()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["fix"]) == ("not_installed", _install_fix("claude-code"))


def _signed_in_unchecked(provider):
    """The vendor's Default, signed in, before its first discovery has a result."""
    from connections.vendor_accounts import set_account_state
    from vendor_account_fixtures import seed_default_account

    account = seed_default_account(provider)
    return set_account_state(account["id"], "signed_in", expected_revision=account["revision"])


def _install_fix(provider):
    from api.provider_sources import _HARNESS_SOURCES

    return _HARNESS_SOURCES[provider][1]



def _stream(*frames):
    return "".join(json.dumps({"type": "control_response", "response": f}) + "\n"
                   for f in frames).encode()


def _ok(request_id, payload=None):
    return {"subtype": "success", "request_id": request_id, "response": payload or {}}


def _err(request_id, text):
    return {"subtype": "error", "request_id": request_id, "error": text}


def _listed(*, usage=None):
    """A control stream whose list_models and get_usage both succeeded."""
    return _stream(_ok("corsia-model-list", {"models": []}),
                   _ok("corsia-usage", usage if usage is not None else {}))


@pytest.fixture
def claude_session(monkeypatch):
    """Claude Code's catalog session is faked; ``sessions`` counts the processes it started."""
    import connections.claude_discovery as claude_discovery
    import connections.harness_models as harness_models

    sessions: list[str] = []
    homes: list[str] = []
    outcome: list = [_listed()]

    async def control(_binary, frames, *, operation, context=None):
        sessions.append(operation)
        homes.append(context.source_home)
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    async def version(_binary, *, env=None):
        return "2.0.0"


    monkeypatch.setattr(claude_discovery, "run_claude_control", control)
    monkeypatch.setattr(harness_models, "binary_version", version)
    monkeypatch.setattr("connections.resolve_builtin_binary",
                        lambda provider, *, db=None: ({}, f"/bin/{provider}"))

    def answer(result):
        outcome[0] = result
        return sessions

    answer.sessions = sessions
    answer.homes = homes
    return answer


@pytest.mark.asyncio
async def test_claude_source_without_an_account_needs_signin_without_a_session(isolated_db, client, claude_session):
    from connections.source_state import check_harness_source

    _put('claude-code', 'available')
    await check_harness_source('claude-code')
    assert _sources(client)['claude-code']['state'] == 'needs_auth'
    assert claude_session.sessions == []


@pytest.mark.real_harness_cli
class TestClaudeSignIn:
    """Source checks use the signed-in account's catalog session."""

    @pytest.fixture(autouse=True)
    def signed_in_account(self, isolated_db):
        from connections.vendor_accounts import set_account_state
        from vendor_account_fixtures import seed_default_account

        account = seed_default_account('claude-code')
        return set_account_state(account['id'], 'signed_in', expected_revision=account['revision'])

    def _recheck(self):
        from connections.source_state import check_harness_source

        asyncio.run(check_harness_source("claude-code"))

    def test_a_session_that_lists_models_is_ready_and_starts_one_process(
        self, isolated_db, client, claude_session, signed_in_account,
    ):
        _put("claude-code", "needs_auth")
        sessions = claude_session(_listed())
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"], claude["fix"]) == ("ready", None, None)
        assert claude["checked_at"]
        assert sessions == ["list_models"]
        assert claude_session.homes == [signed_in_account['source_home']]

    @pytest.mark.parametrize("outcome", [
        RuntimeError("Claude Code list_models failed: Not logged in"),
        _stream(_err("corsia-model-list", "Not logged in · Please run /login")),
        RuntimeError("Claude Code list_models timed out"),
        _stream(_err("corsia-model-list", "upstream 503")),
        _stream(_ok("corsia-usage")),
        OSError("no such file"),
    ])
    def test_failed_account_discovery_is_unverified_without_leaking_vendor_output(
        self, isolated_db, client, claude_session, outcome, signed_in_account,
    ):
        from connections.vendor_accounts import get_account

        _put("claude-code", "available")
        claude_session(outcome)
        self._recheck()
        claude = _sources(client)["claude-code"]
        # A first check that fails has no earlier ready result to fall back on.
        assert (claude["state"], claude["prior_state"], claude["reason"]) == (
            "unverified", None, "Check failed",
        )
        assert get_account(signed_in_account['id'])['state'] == 'signed_in'
        assert claude_session.sessions == ['list_models']

    def test_a_failure_that_only_sounds_like_a_sign_out_keeps_the_row_available(
        self, isolated_db, client, claude_session,
    ):
        from connections.models import connection_available
        from connections.vendor_accounts import get_account

        _put("claude-code", "available")
        claude_session(_listed())
        self._recheck()
        claude_session(_stream(_err(
            "corsia-model-list", "HTTP 401 from proxy; usage limit reached, run /login again")))

        self._recheck()
        claude = _sources(client)["claude-code"]
        assert claude["state"] == "unverified"
        assert claude["prior_state"] == "ready"
        assert connection_available("claude-code") is True
        assert get_account(default_id("claude-code"))["state"] == "signed_in"
        assert claude_session.sessions == ["list_models", "list_models"]

    def test_an_expired_unrefreshable_catalog_is_unverified_and_recovers(
        self, isolated_db, client, claude_session,
    ):
        _put("claude-code", "available")
        claude_session(RuntimeError(
            "Claude Code list_models failed: Failed to authenticate: OAuth session expired and could not be refreshed",
        ))


        self._recheck()

        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"]) == (
            "unverified", "Check failed",
        )

        claude_session(_listed())
        self._recheck()
        assert _sources(client)["claude-code"]["state"] == "ready"

    @pytest.mark.asyncio
    async def test_an_expired_login_on_a_turn_without_an_account_leaves_the_signed_in_account_ready(
        self, isolated_db, client, claude_session, monkeypatch,
    ):
        """A turn with no account receipt cannot say whose login expired, so no account's state changes."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from core.turn_core import TurnIdentity, TurnTerminalResult
        from subagents import cc_event_result
        from subagents.cc_event_context import CCEventContext
        from subagents.cc_runtime_state import _Live
        from vendor_account_fixtures import signed_in_default

        _put("claude-code", "available")
        signed_in_default("claude-code")
        claude_session(RuntimeError(
            "Claude Code list_models failed: Failed to authenticate: OAuth session expired and could not be refreshed",
        ))
        identity = TurnIdentity("account-less-turn", 1)
        live = _Live(session=None, parent_thread_id=None, name="cc", corsia_turn=identity)
        live.turn_active = True
        ctx = CCEventContext(live, "account-less-thread", lambda *_args: None, None)
        monkeypatch.setattr(cc_event_result, "_flush_pending_reasoning", AsyncMock())
        monkeypatch.setattr(
            cc_event_result, "claim_terminal",
            AsyncMock(return_value=SimpleNamespace(result=TurnTerminalResult.APPLIED)),
        )
        monkeypatch.setattr(cc_event_result, "_insert_result_marker", AsyncMock(return_value=("marker", "now")))
        monkeypatch.setattr(cc_event_result, "_announce_result_marker", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(cc_event_result, "_finish_result_postprocessing", AsyncMock())
        monkeypatch.setattr(cc_event_result, "_record_plan_error", AsyncMock())
        monkeypatch.setattr(cc_event_result, "_schedule_sparse_usage_control_read", lambda _ctx: None)

        before = asyncio.all_tasks()
        await cc_event_result.on_result(ctx, {
            "_corsia_turn_identity": identity,
            "is_error": True,
            "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        })
        await asyncio.gather(*(asyncio.all_tasks() - before - {asyncio.current_task()}))

        assert _sources(client)["claude-code"]["state"] == "ready"
        assert claude_session.sessions == []

    def test_an_unverified_session_keeps_a_ready_row_usable(
        self, isolated_db, client, claude_session,
    ):
        from connections.models import connection_available

        _put("claude-code", "available")
        claude_session(_listed())
        self._recheck()
        claude_session(RuntimeError("Claude Code list_models timed out"))
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["prior_state"]) == ("unverified", "ready")
        assert connection_available("claude-code") is True

    @pytest.mark.parametrize(("failure", "reason"), [
        ("claude --version failed: bad install", "Check failed: claude --version failed: bad install"),
        ("claude --version timed out", "Check timed out"),
    ])
    def test_not_checked_yet_ends_in_the_version_reads_failure(
        self, isolated_db, client, claude_session, monkeypatch, failure, reason,
    ):
        import connections.harness_models as harness_models

        async def no_version(_binary, *, env=None):
            raise RuntimeError(failure)

        _put("claude-code", "available")
        assert _sources(client)["claude-code"]["reason"] == "Not checked yet"
        claude_session(_listed())
        monkeypatch.setattr(harness_models, "binary_version", no_version)
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["prior_state"], claude["reason"]) == ("unverified", None, reason)
        assert claude["checked_at"]
        assert claude_session.sessions == []

    def test_a_catalog_without_usage_does_not_clear_the_accounts_limit(
        self, isolated_db, client, claude_session,
    ):
        from connections import harness_usage

        _put("claude-code", "needs_auth")
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        harness_usage.record_usage(provider="claude-code", verdict={
            "state": "limited", "reason": "five_hour_limit", "resets_at": reset,
            "source": "discovery"}, receipt=default_receipt("claude-code"))
        assert harness_usage.active_limit(provider="claude-code", account_id=default_id("claude-code"))
        claude_session(_listed())
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert claude["state"] == "ready" and claude["limited"] is True
        assert harness_usage.active_limit(provider="claude-code", account_id=default_id("claude-code"))

    def test_a_new_account_that_is_itself_limited_stays_limited(
        self, isolated_db, client, claude_session,
    ):
        from connections import harness_usage

        _put("claude-code", "needs_auth")
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        claude_session(_listed(usage={"rate_limits": {
            "five_hour": {"utilization": 100, "resets_at": reset}}}))
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert claude["state"] == "ready" and claude["limited"] is True
        assert harness_usage.active_limit(provider="claude-code", account_id=default_id("claude-code"))

    def test_startup_starts_one_session_for_the_catalog_and_the_sign_in(
        self, isolated_db, client, claude_session,
    ):
        import app_lifecycle

        _put("claude-code", "available")
        sessions = claude_session(_listed())
        asyncio.run(app_lifecycle._startup_harness_discovery())
        assert sessions == ["list_models"]
        assert _sources(client)["claude-code"]["state"] == "ready"

    def test_startup_without_a_signed_in_account_needs_signin(self, isolated_db, client, claude_session, signed_in_account):
        import app_lifecycle
        from connections.vendor_accounts import set_account_state

        _put("claude-code", "available")
        set_account_state(signed_in_account['id'], 'signed_out', expected_revision=signed_in_account['revision'])
        asyncio.run(app_lifecycle._startup_harness_discovery())
        assert claude_session.sessions == []
        assert _sources(client)["claude-code"]["state"] == "needs_auth"

    def test_startup_rechecks_a_signed_out_row_that_the_catalog_refresh_skips(
        self, isolated_db, client, claude_session,
    ):
        import app_lifecycle

        _put("claude-code", "needs_auth")
        sessions = claude_session(_listed())
        asyncio.run(app_lifecycle._startup_harness_discovery())
        assert sessions == ["list_models"]
        assert _sources(client)["claude-code"]["state"] == "ready"

    def test_a_failed_catalog_refresh_keeps_the_account_signed_in(
        self, isolated_db, client, claude_session, signed_in_account,
    ):
        import connections.harness_models as harness_models
        from connections.vendor_accounts import get_account

        _put("claude-code", "available")
        claude_session(RuntimeError("Claude Code list_models failed: Not logged in"))
        result = asyncio.run(harness_models.refresh_provider("claude-code", reason="stale"))
        assert result["ok"] is False
        assert get_account(signed_in_account["id"])["state"] == "signed_in"


async def _noop(*_a, **_k):
    return None


class TestHarnessCheck:
    @pytest.fixture(autouse=True)
    def installed(self, monkeypatch):
        """The CLI is found wherever the test says, never by looking at this machine."""
        found = {"binary": "found"}
        monkeypatch.setattr("connections.resolve_builtin_binary",
                            lambda provider, *, db=None: ({}, f"/bin/{provider}" if found["binary"] else None))
        return found

    def _check(self, monkeypatch, provider, result):
        from connections.source_state import check_harness_source

        async def refresh(_provider, **_kwargs):
            return {"ok": result[0] == "ready", "error": (
                "account_not_configured" if result[0] == "needs_auth" else None)}

        monkeypatch.setattr("connections.harness_models.refresh_provider", refresh)
        asyncio.run(check_harness_source(provider))

    def test_a_codex_source_check_does_not_clear_an_existing_limit(
        self, isolated_db, client, monkeypatch,
    ):
        from connections import harness_usage
        from vendor_account_fixtures import signed_in_default

        signed_in_default("codex")
        _put("codex", "available")
        harness_usage.record_usage(provider="codex", verdict={
            "state": "limited", "reason": "five_hour_limit",
            "resets_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "source": "discovery"}, receipt=default_receipt("codex"))
        self._check(monkeypatch, "codex", ("ready", None))
        codex = _sources(client)["codex"]
        assert codex["state"] == "ready" and codex["limited"] is True
        assert harness_usage.active_limit(provider="codex", account_id=default_id("codex"))

    def test_a_recheck_that_still_finds_no_cli_stays_not_installed_and_refreshes_nothing(
        self, isolated_db, client, monkeypatch, installed,
    ):
        from vendor_account_fixtures import signed_in_default

        signed_in_default("codex")
        installed["binary"] = None
        _put("codex", "not_installed")
        self._check(monkeypatch, "codex", ("ready", None))
        assert _sources(client)["codex"]["state"] == "not_installed"

    def test_unrecordable_refresh_failure_reaches_the_caller(
        self, isolated_db, client, monkeypatch,
    ):
        import connections.harness_models as harness_models
        from connections.source_state import check_harness_source

        _put("claude-code", "available")

        async def boom(*_a, **_k):
            raise ValueError("boom")

        monkeypatch.setattr(harness_models, "refresh_provider", boom)
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(check_harness_source("claude-code"))
        assert _sources(client)["claude-code"]["state"] == "needs_auth"


def _server(status="online", kind="managed", provider="omlx", name="Studio", **meta):
    from infra.config import STATE_ROOT
    from llm import network_discovery as nd

    managed = provider == "omlx" and kind == "managed"
    sid = nd.upsert_server(
        provider, "corsia-managed" if managed else f"{name}-inst", name, "127.0.0.1", 8000, status, kind=kind,
    )
    if managed:
        meta = {
            "corsia_managed": True,
            "base_path": str(STATE_ROOT / "managed-omlx"),
            "model_dir": str(STATE_ROOT / "managed-omlx" / "models"),
            **meta,
        }
    if meta:
        nd.set_server_metadata(sid, **meta)
    return sid


def _model(litellm_model, *, enabled=1, available=1):
    from infra.database import write_transaction

    now = datetime.now(timezone.utc).isoformat()
    with write_transaction(None, scope="test_source_model") as db:
        db.execute(
            "INSERT INTO models (id, provider, provider_model_id, litellm_model, display_name,"
            " enabled, available, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (litellm_model, litellm_model.split("@")[0], litellm_model, litellm_model,
             litellm_model, enabled, available, now, now),
        )


@pytest.mark.usefixtures("mac_local_model_host")
class TestOmlx:
    @pytest.fixture(autouse=True)
    def reset_local_flight(self, monkeypatch):
        from llm import local_setup

        monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())

    def test_no_binary_is_not_installed(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: None)
        omlx = _sources(client)["omlx"]
        assert omlx["state"] == "not_installed" and omlx["name"] == "Local model"

    def test_the_bundled_binary_with_no_server_needs_setup_not_install(
        self, isolated_db, client, monkeypatch,
    ):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: None)
        monkeypatch.setattr(
            "llm.local_sources.managed_omlx_executable",
            lambda: "/Applications/CORSIA.app/Contents/Resources/bin/omlx",
        )
        omlx = _sources(client)["omlx"]
        assert (omlx["state"], omlx["reason"]) == ("needs_setup", "No local model server is set up")

    def test_a_reachable_explicit_external_endpoint_is_ready_without_a_local_binary(
        self, isolated_db, client, monkeypatch,
    ):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: None)
        sid = _server(provider="external", kind="external", name="Studio")
        _model(f"external@{sid}/qwen")

        sources = _sources(client)
        assert sources["omlx"]["state"] == "not_installed"
        assert sources[f"external:{sid}"]["state"] == "ready"

    def test_an_offline_server_without_the_binary_needs_setup_not_install(
        self, isolated_db, client, monkeypatch,
    ):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: None)
        _server(status="offline")
        omlx = _sources(client)["omlx"]
        assert omlx["state"] == "needs_setup" and omlx["fix"] != "Install oMLX"

    def test_binary_without_a_managed_row_needs_setup(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        omlx = _sources(client)["omlx"]
        assert omlx["state"] == "needs_setup" and omlx["fix"] == "Settings › Providers › Local model"

    def test_server_that_is_not_answering_needs_setup(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        _server(status="offline")
        assert _sources(client)["omlx"]["state"] == "needs_setup"

    def test_online_server_without_models_needs_setup(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        _server()
        assert _sources(client)["omlx"]["state"] == "needs_setup"

    def test_online_server_with_a_model_is_ready(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        sid = _server()
        _model(f"omlx@{sid}/qwen")
        omlx = _sources(client)["omlx"]
        assert (omlx["state"], omlx["fix"], omlx["reason"]) == ("ready", None, None)

    def test_online_installed_model_stays_ready_at_24_gib(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_setup_gates.host_ram_bytes", lambda: 24 * 1024**3)
        sid = _server()
        _model(f"omlx@{sid}/qwen")

        omlx = _sources(client)["omlx"]

        assert omlx["eligible"] is False
        assert omlx["state"] == "ready"

    def test_an_external_endpoint_never_counts_as_the_local_model(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        sid = _server(provider="external", kind="external", name="Lab")
        _model(f"external@{sid}/m")
        assert _sources(client)["omlx"]["state"] == "needs_setup"


class TestExternal:
    def test_online_with_an_enabled_model_is_ready(self, isolated_db, client):
        sid = _server(provider="external", kind="external", name="Lab GPU")
        _model(f"external@{sid}/m")
        source = _sources(client)[f"external:{sid}"]
        assert source["name"] == "Lab GPU" and source["state"] == "ready"
        assert set(source) == _KEYS

    def test_offline_endpoint_is_unverified_with_its_stored_reason(self, isolated_db, client):
        sid = _server(status="offline", provider="external", kind="external",
                      discovery={"at": "2026-09-30T20:00:00Z", "models": 0,
                                 "error": "Connection failed: refused"})
        source = _sources(client)[f"external:{sid}"]
        assert source["state"] == "unverified" and source["fix"] == "Check again"
        assert source["reason"] == "Connection failed: refused"
        assert source["checked_at"] == "2026-09-30T20:00:00Z"

    def test_online_endpoint_with_only_disabled_models_is_unverified(self, isolated_db, client):
        sid = _server(provider="external", kind="external")
        _model(f"external@{sid}/m", enabled=0)
        assert _sources(client)[f"external:{sid}"]["state"] == "unverified"

    def test_disabled_endpoint_is_left_out(self, isolated_db, client):
        sid = _server(status="disabled", provider="external", kind="external")
        assert f"external:{sid}" not in _sources(client)


class TestLimited:
    def test_ready_harness_with_a_limited_verdict_adds_reset(self, isolated_db, client):
        from connections import harness_usage
        from vendor_account_fixtures import signed_in_default

        _put("codex", "available")
        signed_in_default("codex")
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        harness_usage.record_usage(provider="codex", verdict={
            "state": "limited", "reason": "five_hour_limit", "resets_at": reset,
            "source": "discovery"}, receipt=default_receipt("codex"))
        codex = _sources(client)["codex"]
        assert codex["state"] == "ready" and codex["limited"] is True
        assert codex["resets_at"] == reset

    def test_unlimited_source_has_no_limited_key(self, isolated_db, client):
        from vendor_account_fixtures import signed_in_default

        _put("codex", "available")
        signed_in_default("codex")
        assert _sources(client)["codex"]["state"] == "ready"
        assert "limited" not in _sources(client)["codex"]


@pytest.mark.usefixtures("mac_local_model_host")
class TestRecheck:
    def test_requires_a_signed_in_caller(self, isolated_db):
        from api.provider_sources import router

        app = FastAPI()
        app.include_router(router)
        bare = TestClient(app)
        assert bare.get("/api/providers/sources").status_code == 401
        assert bare.post("/api/providers/sources/recheck").status_code == 401

    def test_local_setup_route_returns_the_current_background_stage(self, isolated_db, client, monkeypatch):
        result = {"ok": True, "in_progress": True, "stage": "Downloading model (8.60 GiB)"}
        calls = []

        async def begin(**kwargs):
            calls.append(kwargs)
            return result

        monkeypatch.setattr("llm.local_setup_flow.begin_local_setup", begin)
        assert client.post("/api/providers/local/setup").json() == result
        assert calls == [{"no_subscription_only": False, "automatic": False, "onboarding": False}]
        assert client.post("/api/providers/local/setup", json={
            "no_subscription_only": True, "automatic": True,
        }).json() == result
        assert calls[-1] == {"no_subscription_only": True, "automatic": True, "onboarding": False}
        assert client.post("/api/providers/local/setup", json={"onboarding": True}).status_code == 200
        assert calls[-1]["onboarding"] is True

    def test_local_cancel_route_uses_the_canonical_flight_owner(self, isolated_db, client, monkeypatch):
        calls = []

        async def cancel():
            calls.append(True)
            return {"ok": True, "cancelled": True}

        monkeypatch.setattr("llm.local_setup.cancel_local_setup", cancel)
        assert client.post("/api/providers/local/cancel").json() == {"ok": True, "cancelled": True}
        assert calls == [True]

    def test_local_setup_status_route_returns_the_flight_record(self, isolated_db, client, monkeypatch):
        import asyncio

        from llm import local_model, local_setup

        monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
        monkeypatch.setattr("llm.local_setup_prepare.local_memory_ok", lambda *_args: True)
        # Status is derived from the managed local server and flight record.
        not_checked = {"state": "not_checked", "model": None, "message": None}
        idle = client.get("/api/providers/local/setup").json()
        assert idle["state"] == "stopped" and idle["local_model"] == not_checked

        failure = {"ok": False, "message": "oMLX failed"}
        local_setup._flight.result = failure
        failed = client.get("/api/providers/local/setup").json()
        assert failed["state"] == "failed" and failed["local_model"] == not_checked
        assert failed["fix"] == "CORSIA couldn’t set up the local model." and idle["fix"] is None

        # A finished setup's model is what the conductor's offer and Welcome read.
        installed = local_model.LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 262_144)
        local_model.record_set_up(installed)
        assert client.get("/api/providers/local/setup").json()["local_model"] == {
            "state": "picked", "model": installed.view(), "message": None}

        local_setup._flight.result = {"ok": True}
        assert client.get("/api/providers/local/setup").json()["state"] == "ready"

        async def running():
            local_setup._flight.task = asyncio.create_task(asyncio.sleep(10))
            local_setup._flight.stage = "Downloading"
            status = local_setup.setup_status()
            local_setup._flight.task.cancel()
            return status

        running_status = asyncio.run(running())
        assert running_status["state"] == "starting"

    def test_local_setup_status_route_requires_a_signed_in_caller(self, isolated_db):
        from api.provider_sources import router

        app = FastAPI()
        app.include_router(router)
        assert TestClient(app).get("/api/providers/local/setup").status_code == 401
        assert TestClient(app).post("/api/providers/local/cancel").status_code == 401

    def test_reruns_the_existing_checks_and_returns_the_list(self, isolated_db, client, monkeypatch):
        from llm import network_discovery as nd
        from api import provider_sources

        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        managed = _server(status="offline")
        external = _server(status="offline", provider="external", kind="external", name="Lab")
        parked = _server(status="disabled", provider="external", kind="external", name="Off")
        calls: list = []

        async def check(provider):
            calls.append(("check", provider))

        async def sweep(_client, server):
            calls.append(("sweep", server["id"]))
            nd.update_server_status(server["id"], status="online")
            _model(f"omlx@{server['id']}/qwen")

        async def test_endpoint(server_id, _server):
            calls.append(("test", server_id))
            return {"ok": True, "message": "Reachable. 1 model found."}

        monkeypatch.setattr(provider_sources, "check_harness_source", check)
        monkeypatch.setattr(nd, "_sweep_one", sweep)
        monkeypatch.setattr("api.external_connection_check.check_external_server", test_endpoint)

        body = client.post("/api/providers/sources/recheck").json()

        assert sorted(calls, key=str) == sorted([
            ("check", "claude-code"), ("check", "codex"),
            ("sweep", managed), ("test", external)], key=str)
        assert parked not in {c[1] for c in calls}
        by_source = {s["source"]: s for s in body["sources"]}
        assert by_source["omlx"]["state"] == "ready"
        assert all(set(s) >= _KEYS for s in body["sources"])
        assert [s["source"] for s in body["sources"][:3]] == ["claude-code", "codex", "omlx"]

    def test_one_unrecorded_check_fails_the_route(self, isolated_db, client, monkeypatch):
        from api import provider_sources

        async def broken(provider):
            raise RuntimeError("probe crashed")

        monkeypatch.setattr(provider_sources, "check_harness_source", broken)
        response = client.post("/api/providers/sources/recheck")
        assert response.status_code == 503
        assert "sources" not in response.json()

    def test_external_recheck_probes_an_endpoint_stored_as_offline(
        self, isolated_db, client, monkeypatch,
    ):
        """Check again takes the Test path: probe, sweep, discovery."""
        from llm import network_discovery as nd
        import api.external_connection_check as check

        sid = _server(status="offline", provider="external", kind="external", name="Lab",
                      base_url="http://lab.lan:8000")
        steps: list = []

        async def probe(*_a, **_k):
            steps.append("probe")

        async def sweep(_client, server):
            steps.append("sweep")
            nd.update_server_status(server["id"], status="online")
            _model(f"external@{server['id']}/m")

        async def discover(_server_id):
            steps.append("discover")
            return {"ok": True, "discovered": 1, "updated": 0}

        async def no_images(_server_id):
            return False

        monkeypatch.setattr(check, "_probe_external", probe)
        monkeypatch.setattr(nd, "_sweep_one", sweep)
        monkeypatch.setattr(nd, "_discover_and_notify", discover)
        monkeypatch.setattr(check, "detect_image_input", no_images)
        assert _sources(client)[f"external:{sid}"]["state"] == "unverified"

        body = client.post("/api/providers/sources/recheck").json()

        assert steps == ["probe", "sweep", "discover"]
        by_source = {s["source"]: s for s in body["sources"]}
        assert by_source[f"external:{sid}"]["state"] == "ready"

    def test_a_failed_external_step_fails_the_request_without_overriding_stored_facts(
        self, isolated_db, client, monkeypatch,
    ):
        sid = _server(provider="external", kind="external", name="Lab")
        _model(f"external@{sid}/m")
        assert _sources(client)[f"external:{sid}"]["state"] == "ready"

        async def broken(_server_id, _server):
            raise RuntimeError("database is locked\ntraceback")

        monkeypatch.setattr("api.external_connection_check.check_external_server", broken)
        response = client.post("/api/providers/sources/recheck")
        assert response.status_code == 503
        assert "sources" not in response.json()
        assert "database" not in response.text
        assert _sources(client)[f"external:{sid}"]["state"] == "ready"

        async def fine(_server_id, _server):
            return {"ok": True, "message": "Reachable. 1 model found."}

        monkeypatch.setattr("api.external_connection_check.check_external_server", fine)
        client.post("/api/providers/sources/recheck")
        assert _sources(client)[f"external:{sid}"]["state"] == "ready"

    def test_an_unreachable_endpoint_fails_the_request_with_a_safe_message(
        self, isolated_db, client, monkeypatch,
    ):
        sid = _server(provider="external", kind="external", name="Lab")
        _model(f"external@{sid}/m")

        async def refused(_server_id, _server):
            return {"ok": False, "message": "Could not reach lab.lan: refused"}

        monkeypatch.setattr("api.external_connection_check.check_external_server", refused)
        response = client.post("/api/providers/sources/recheck")
        assert response.status_code == 503
        assert "sources" not in response.json()
        assert "lab.lan" not in response.text

    def test_a_failed_oMLX_sweep_fails_the_request_without_an_overlay(
        self, isolated_db, client, monkeypatch,
    ):
        from llm import network_discovery as nd

        monkeypatch.setattr("llm.local_sources.omlx_on_path", lambda: "/bin/omlx")
        sid = _server()
        _model(f"omlx@{sid}/qwen")
        assert _sources(client)["omlx"]["state"] == "ready"

        async def broken(_client, _server):
            raise OSError("network is down")

        monkeypatch.setattr(nd, "_sweep_one", broken)
        response = client.post("/api/providers/sources/recheck")
        assert response.status_code == 503
        assert "sources" not in response.json()
        assert _sources(client)["omlx"]["state"] == "ready"


class TestStartupTrigger:
    def test_startup_only_refreshes_the_signed_in_accounts_catalogs(self, monkeypatch):
        import app_lifecycle
        import connections.harness_refresh as harness_refresh

        calls: list = []

        async def refresh(*, reason):
            calls.append(("refresh", reason))
            return [{"ok": True}]

        monkeypatch.setattr(harness_refresh, "refresh_harness_catalogs", refresh)
        assert asyncio.run(app_lifecycle._startup_harness_discovery()) == [{"ok": True}]
        assert calls == [("refresh", "startup")]

    def test_startup_under_the_unit_test_guard_starts_no_host_cli(self, isolated_db, monkeypatch):
        """The sign-in check never reaches a host harness CLI from a unit test (#2792)."""
        import app_lifecycle
        import connections.harness_refresh as harness_refresh

        _put("claude-code", "available")
        _put("codex", "available")
        spawned: list = []

        async def spawn(*argv, **_kwargs):
            spawned.append(argv)
            raise FileNotFoundError(argv[0])

        def run(*argv, **_kwargs):
            spawned.append(argv)
            raise FileNotFoundError(argv[0])

        async def refresh(*, reason):
            return []

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(harness_refresh, "refresh_harness_catalogs", refresh)
        asyncio.run(app_lifecycle._startup_harness_discovery())
        assert spawned == []
