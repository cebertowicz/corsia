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


async def _no_check(_provider, *, rediscover=False):
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


def _completed(code, out=b"", err=b""):
    return subprocess.CompletedProcess([], code, out, err)


class TestCodexLogin:
    @pytest.mark.parametrize(("result", "state"), [
        (_completed(0, b"Logged in using ChatGPT"), "ready"),
        (_completed(1, err=b"Not logged in"), "needs_auth"),
        (_completed(1, err=b"error: database is locked"), "unverified"),
        (subprocess.TimeoutExpired("codex", 10), "unverified"),
        (OSError("exec format error"), "unverified"),
    ])
    def test_three_way(self, monkeypatch, result, state):
        import connections

        def run(*_a, **_k):
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(subprocess, "run", run)
        assert connections._codex_sign_in_state("codex") == state


class TestSeedCodex:
    def _seed(self, monkeypatch, probe):
        import connections

        monkeypatch.setattr("shutil.which", lambda name, *a, **k: f"/bin/{name}")
        monkeypatch.setattr(connections, "_gh_status", lambda _binary: (True, None))
        monkeypatch.setattr(connections, "_codex_sign_in_state", lambda _b: probe)
        connections.seed_builtin_connections()

    def test_timeout_on_a_ready_row_keeps_it_usable(self, isolated_db, client, monkeypatch):
        from connections.models import connection_available

        self._seed(monkeypatch, "ready")
        self._seed(monkeypatch, "unverified")
        codex = _sources(client)["codex"]
        assert (codex["state"], codex["prior_state"]) == ("unverified", "ready")
        assert codex["fix"] == "Check again" and codex["reason"] and codex["checked_at"]
        assert connection_available("codex") is True

    def test_timeout_on_a_signed_out_row_stays_unusable(self, isolated_db, client, monkeypatch):
        from connections.models import connection_available, get_connection

        self._seed(monkeypatch, "needs_auth")
        self._seed(monkeypatch, "unverified")
        codex = _sources(client)["codex"]
        assert (codex["state"], codex["prior_state"]) == ("unverified", "needs_auth")
        assert get_connection("codex")["status"] == "unverified"
        assert connection_available("codex") is False

    def test_consecutive_timeouts_keep_the_last_definite_state(self, isolated_db, client, monkeypatch):
        self._seed(monkeypatch, "needs_auth")
        self._seed(monkeypatch, "unverified")
        self._seed(monkeypatch, "unverified")
        assert _sources(client)["codex"]["prior_state"] == "needs_auth"

    def test_each_definite_state(self, isolated_db, client, monkeypatch):
        self._seed(monkeypatch, "needs_auth")
        assert _sources(client)["codex"]["fix"] == "Connect it in the app."
        self._seed(monkeypatch, "ready")
        codex = _sources(client)["codex"]
        assert (codex["state"], codex["fix"], codex["prior_state"]) == ("ready", None, None)
        monkeypatch.setattr("shutil.which", lambda name, *a, **k: None if name == "codex" else "/bin/x")
        monkeypatch.setattr("os.path.isfile", lambda p: False)
        import connections

        connections.seed_builtin_connections()
        assert _sources(client)["codex"]["state"] == "not_installed"

    def test_seeded_claude_is_not_ready_until_a_probe_says_so(self, isolated_db, client, monkeypatch):
        self._seed(monkeypatch, "ready")
        claude = _sources(client)["claude-code"]
        assert claude["state"] == "unverified" and claude["reason"] == "Not checked yet"

    def test_unverified_cli_reports_a_proven_package_manager(self, isolated_db, client, monkeypatch):
        import cli_location

        self._seed(monkeypatch, "ready")
        monkeypatch.setattr(cli_location, "package_manager_for", lambda provider: "homebrew" if provider == "claude-code" else None)

        assert _sources(client)["claude-code"]["cli_package_manager"] == "homebrew"

    def test_reseed_keeps_the_last_claude_verdict(self, isolated_db, client, monkeypatch):
        from connections.models import get_connection
        from connections.source_state import apply_check

        self._seed(monkeypatch, "ready")
        apply_check("claude-code", "needs_auth", "signed out")
        self._seed(monkeypatch, "ready")
        assert get_connection("claude-code")["status"] == "needs_auth"
        assert _sources(client)["claude-code"]["state"] == "needs_auth"



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
    import connections.source_state as source_state

    sessions: list[str] = []
    outcome: list = [_listed()]

    async def control(_binary, frames, *, operation):
        sessions.append(operation)
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    async def version(_binary):
        return "2.0.0"

    # `claude auth status`: an exit code, "hang" (never answers) or an exception.
    auth: list = [0]
    auth_calls: list[tuple] = []

    class _AuthProcess:
        returncode = None

        async def wait(self):
            if auth[0] == "hang":
                await asyncio.sleep(60)
            return auth[0]

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            return b"", b""

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        if tuple(args[1:3]) != ("auth", "status"):
            return await real_exec(*args, **kwargs)
        auth_calls.append(args)
        # The command's output must never be captured: it carries the account's email.
        assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        if isinstance(auth[0], BaseException):
            raise auth[0]
        return _AuthProcess()

    monkeypatch.setattr(claude_discovery.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(claude_discovery, "AUTH_STATUS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(claude_discovery, "run_claude_control", control)
    monkeypatch.setattr(harness_models, "binary_version", version)
    monkeypatch.setattr("connections.resolve_builtin_binary",
                        lambda provider, *, db=None: ({}, f"/bin/{provider}"))
    # The suite's startup stub is lifted here: the only session is the faked one.
    monkeypatch.setattr("connections.startup.check_harness_source",
                        source_state.check_harness_source)

    def answer(result):
        outcome[0] = result
        return sessions

    def auth_status(result):
        auth[0] = result

    answer.sessions = sessions
    answer.auth_status = auth_status
    answer.auth_calls = auth_calls
    return answer


@pytest.mark.real_harness_cli
class TestClaudeSignIn:
    """The catalog session is the one Claude Code sign-in check (#1910)."""

    def _recheck(self):
        from connections.source_state import check_harness_source

        asyncio.run(check_harness_source("claude-code", rediscover=True))

    def test_a_session_that_lists_models_is_ready_and_starts_one_process(
        self, isolated_db, client, claude_session,
    ):
        _put("claude-code", "needs_auth")
        sessions = claude_session(_listed())
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"], claude["fix"]) == ("ready", None, None)
        assert claude["checked_at"]
        assert sessions == ["list_models"]

    @pytest.mark.parametrize(("outcome", "auth", "state", "reason"), [
        (RuntimeError("Claude Code list_models failed: Not logged in"),
         1, "needs_auth", "Not signed in"),
        (_stream(_err("corsia-model-list", "Not logged in · Please run /login")),
         1, "needs_auth", "Not signed in"),
        (RuntimeError("Claude Code list_models timed out"), 0, "unverified", "Check timed out"),
        (_stream(_err("corsia-model-list", "upstream 503")),
         0, "unverified", "Check failed: upstream 503"),
        (_stream(_ok("corsia-usage")), 0, "unverified",
         "Check failed: Claude Code returned no reply"),
        (OSError("no such file"), OSError("no such file"), "unverified",
         "Check failed: no such file"),
    ])
    def test_each_failed_session_reads_in_the_one_vocabulary(
        self, isolated_db, client, claude_session, outcome, auth, state, reason,
    ):
        _put("claude-code", "available")
        claude_session(outcome)
        claude_session.auth_status(auth)
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"]) == (state, reason)

    def test_a_failure_that_only_sounds_like_a_sign_out_keeps_the_row_available(
        self, isolated_db, client, claude_session,
    ):
        from connections.models import connection_available

        _put("claude-code", "available")
        claude_session(_stream(_err(
            "corsia-model-list", "HTTP 401 from proxy; usage limit reached, run /login again")))
        claude_session.auth_status(0)  # `claude auth status`: signed in
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert claude["state"] == "unverified"
        assert claude["prior_state"] == "ready"
        assert connection_available("claude-code") is True
        assert len(claude_session.auth_calls) == 1

    def test_an_expired_unrefreshable_login_marks_the_source_needing_auth_and_recovers(
        self, isolated_db, client, claude_session,
    ):
        _put("claude-code", "available")
        claude_session(RuntimeError(
            "Claude Code list_models failed: Failed to authenticate: OAuth session expired and could not be refreshed",
        ))
        claude_session.auth_status(0)  # The CLI still sees cached credentials.

        self._recheck()

        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["reason"]) == (
            "needs_auth", "Not signed in",
        )

        claude_session(_listed())
        self._recheck()
        assert _sources(client)["claude-code"]["state"] == "ready"

    def test_terminal_expired_login_rechecks_then_marks_needs_auth(self, isolated_db, client, claude_session):
        from connections.claude_discovery import reconcile_turn_auth_failure

        _put("claude-code", "available")
        claude_session(RuntimeError(
            "Claude Code list_models failed: Failed to authenticate: OAuth session expired and could not be refreshed",
        ))
        claude_session.auth_status(0)

        asyncio.run(reconcile_turn_auth_failure(
            "Failed to authenticate: OAuth session expired and could not be refreshed",
        ))

        assert _sources(client)["claude-code"]["state"] == "needs_auth"
        assert claude_session.sessions == ["list_models"]

    def test_terminal_expired_login_keeps_a_successful_recheck_ready(
        self, isolated_db, client, claude_session,
    ):
        from connections.claude_discovery import reconcile_turn_auth_failure

        _put("claude-code", "available")
        claude_session(_listed())

        asyncio.run(reconcile_turn_auth_failure(
            "Failed to authenticate: OAuth session expired and could not be refreshed",
        ))

        assert _sources(client)["claude-code"]["state"] == "ready"
        assert claude_session.sessions == ["list_models"]

    @pytest.mark.asyncio
    async def test_result_event_expired_login_rechecks_then_marks_needs_auth(
        self, isolated_db, client, claude_session, monkeypatch,
    ):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from core.turn_core import TurnIdentity, TurnTerminalResult
        from subagents import cc_event_result, cc_runtime_errors
        from subagents.cc_event_context import CCEventContext
        from subagents.cc_runtime_state import _Live

        _put("claude-code", "available")
        claude_session(RuntimeError(
            "Claude Code list_models failed: Failed to authenticate: OAuth session expired and could not be refreshed",
        ))
        claude_session.auth_status(0)
        identity = TurnIdentity("result-error-turn", 1)
        live = _Live(
            session=None, parent_thread_id=None, name="cc", corsia_turn=identity,
        )
        live.turn_active = True
        ctx = CCEventContext(live, "result-error-thread", lambda *_args: None, None)
        monkeypatch.setattr(cc_event_result, "_flush_pending_reasoning", AsyncMock())
        monkeypatch.setattr(
            cc_event_result, "claim_terminal",
            AsyncMock(return_value=SimpleNamespace(result=TurnTerminalResult.APPLIED)),
        )
        monkeypatch.setattr(
            cc_event_result, "_insert_result_marker",
            AsyncMock(return_value=("marker", "now")),
        )
        monkeypatch.setattr(cc_event_result, "_announce_result_marker", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(cc_event_result, "_finish_result_postprocessing", AsyncMock())
        monkeypatch.setattr(cc_event_result, "_record_plan_error", AsyncMock())
        monkeypatch.setattr(
            cc_event_result, "_schedule_sparse_usage_control_read", lambda _ctx: None,
        )

        await cc_event_result.on_result(ctx, {
            "_corsia_turn_identity": identity,
            "is_error": True,
            "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        })
        tasks = tuple(cc_runtime_errors._EXPIRED_LOGIN_RECONCILIATION_TASKS)
        assert tasks, "result handling must retain its reconciliation task"
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)

        assert not set(tasks) & cc_runtime_errors._EXPIRED_LOGIN_RECONCILIATION_TASKS
        assert _sources(client)["claude-code"]["state"] == "needs_auth"
        assert claude_session.sessions == ["list_models"]

    @pytest.mark.parametrize("auth", ["hang", OSError("cannot run"), 2, -9])
    def test_an_auth_status_that_does_not_say_signed_out_proves_nothing(
        self, isolated_db, client, claude_session, auth,
    ):
        _put("claude-code", "available")
        claude_session(RuntimeError("Claude Code list_models failed: Not logged in"))
        claude_session.auth_status(auth)
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["prior_state"]) == ("unverified", "ready")

    def test_a_listed_catalog_asks_auth_status_once_before_ready(
        self, isolated_db, client, claude_session,
    ):
        # A signed-out CLI still lists its static models, so a listed catalog alone
        # never proves the login.
        _put("claude-code", "needs_auth")
        claude_session(_listed())
        self._recheck()
        assert claude_session.auth_calls == [("/bin/claude-code", "auth", "status")]
        assert _sources(client)["claude-code"]["state"] == "ready"

    def test_a_listed_catalog_from_a_signed_out_cli_needs_sign_in(
        self, isolated_db, client, claude_session,
    ):
        _put("claude-code", "ready")
        claude_session(_listed())
        claude_session.auth_status(1)
        self._recheck()
        assert _sources(client)["claude-code"]["state"] == "needs_auth"

    def test_a_state_write_that_fails_is_reported_unverified_not_as_the_old_row(
        self, isolated_db, client, claude_session, monkeypatch,
    ):
        import connections.source_state as source_state

        _put("claude-code", "needs_auth")
        claude_session(_listed())
        real = source_state.apply_check

        def flaky(provider, state, reason=None, *, db=None, schedule_usage_check=True):
            if state == source_state.READY:
                raise OSError("database is locked")
            return real(provider, state, reason, db=db, schedule_usage_check=schedule_usage_check)

        monkeypatch.setattr(source_state, "apply_check", flaky)
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert claude["state"] == "unverified"
        assert claude["prior_state"] == "needs_auth"
        assert claude["reason"] == "Check failed: database is locked"

    def test_a_state_write_that_finds_no_row_is_reported_unverified(
        self, isolated_db, client, claude_session, monkeypatch,
    ):
        import connections.source_state as source_state

        _put("claude-code", "needs_auth")
        claude_session(_listed())
        real = source_state.apply_check
        monkeypatch.setattr(
            source_state, "apply_check",
            lambda provider, state, reason=None, *, db=None, schedule_usage_check=True: (
                None if state == source_state.READY else real(
                    provider, state, reason, db=db, schedule_usage_check=schedule_usage_check)),
        )
        self._recheck()
        assert _sources(client)["claude-code"]["state"] == "unverified"

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

    def test_a_failure_before_the_session_is_still_recorded(
        self, isolated_db, client, claude_session, monkeypatch,
    ):
        import connections.harness_models as harness_models

        async def no_version(_binary):
            raise RuntimeError("claude --version failed: bad install")

        _put("claude-code", "needs_auth")
        claude_session(_listed())
        monkeypatch.setattr(harness_models, "binary_version", no_version)
        self._recheck()
        claude = _sources(client)["claude-code"]
        assert (claude["state"], claude["prior_state"]) == ("unverified", "needs_auth")
        assert claude["reason"] == "Check failed: claude --version failed: bad install"
        assert claude_session.sessions == []

    def test_signing_in_and_checking_again_supersedes_the_old_accounts_limit(
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
        assert claude["state"] == "ready" and "limited" not in claude
        assert harness_usage.active_limit(provider="claude-code", account_id=default_id("claude-code")) is None

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

    def test_startup_rechecks_a_signed_out_row_that_the_catalog_refresh_skips(
        self, isolated_db, client, claude_session,
    ):
        import app_lifecycle

        _put("claude-code", "needs_auth")
        sessions = claude_session(_listed())
        asyncio.run(app_lifecycle._startup_harness_discovery())
        assert sessions == ["list_models"]
        assert _sources(client)["claude-code"]["state"] == "ready"

    def test_a_catalog_refresh_alone_notices_a_sign_out(
        self, isolated_db, client, claude_session,
    ):
        import connections.harness_models as harness_models

        _put("claude-code", "available")
        claude_session(RuntimeError("Claude Code list_models failed: Not logged in"))
        claude_session.auth_status(1)
        asyncio.run(harness_models.refresh_provider("claude-code", reason="stale"))
        assert _sources(client)["claude-code"]["state"] == "needs_auth"


async def _noop(*_a, **_k):
    return None


class TestHarnessCheck:
    def _check(self, monkeypatch, provider, result, *, rediscover=False):
        from connections.source_state import check_harness_source

        monkeypatch.setattr("connections._codex_sign_in_state", lambda _b: result[0])
        asyncio.run(check_harness_source(provider, rediscover=rediscover))

    def test_codex_results_persist(self, isolated_db, client, monkeypatch):
        _put("codex", "available")
        self._check(monkeypatch, "codex", ("needs_auth", None))
        codex = _sources(client)["codex"]
        assert codex["state"] == "needs_auth" and codex["fix"] == "Connect it in the app."
        self._check(monkeypatch, "codex", ("ready", None))
        assert _sources(client)["codex"]["state"] == "ready"

    def test_signing_in_to_codex_and_checking_again_supersedes_the_old_limit(
        self, isolated_db, client, monkeypatch,
    ):
        from connections import harness_usage

        _put("codex", "needs_auth")
        harness_usage.record_usage(provider="codex", verdict={
            "state": "limited", "reason": "five_hour_limit",
            "resets_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "source": "discovery"}, receipt=default_receipt("codex"))
        self._check(monkeypatch, "codex", ("ready", None))
        codex = _sources(client)["codex"]
        assert codex["state"] == "ready" and "limited" not in codex
        assert harness_usage.active_limit(provider="codex", account_id=default_id("codex")) is None

    def test_codex_unverified_reads_check_failed(self, isolated_db, client, monkeypatch):
        _put("codex", "available")
        self._check(monkeypatch, "codex", ("ready", None))
        self._check(monkeypatch, "codex", ("unverified", None))
        codex = _sources(client)["codex"]
        assert (codex["state"], codex["prior_state"], codex["reason"]) == (
            "unverified", "ready", "Check failed")
        self._check(monkeypatch, "codex", ("needs_auth", None))
        assert _sources(client)["codex"]["reason"] == "Not signed in"

    def test_trigger_path_leaves_a_not_installed_row_alone(self, isolated_db, client, monkeypatch):
        _put("codex", "not_installed")
        self._check(monkeypatch, "codex", ("ready", None))
        assert _sources(client)["codex"]["state"] == "not_installed"

    def test_unrecordable_refresh_failure_reaches_the_caller(
        self, isolated_db, client, monkeypatch,
    ):
        import connections.harness_models as harness_models
        from connections.source_state import check_harness_source

        _put("claude-code", "needs_auth")

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


class TestOmlx:
    def test_no_binary_is_not_installed(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: None)
        omlx = _sources(client)["omlx"]
        assert omlx["state"] == "not_installed" and omlx["name"] == "Local model"

    def test_a_reachable_explicit_external_endpoint_is_ready_without_a_local_binary(
        self, isolated_db, client, monkeypatch,
    ):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: None)
        sid = _server(provider="external", kind="external", name="Studio")
        _model(f"external@{sid}/qwen")

        sources = _sources(client)
        assert sources["omlx"]["state"] == "not_installed"
        assert sources[f"external:{sid}"]["state"] == "ready"

    def test_an_offline_server_without_the_binary_needs_setup_not_install(
        self, isolated_db, client, monkeypatch,
    ):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: None)
        _server(status="offline")
        omlx = _sources(client)["omlx"]
        assert omlx["state"] == "needs_setup" and omlx["fix"] != "Install oMLX"

    def test_binary_without_a_managed_row_needs_setup(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
        omlx = _sources(client)["omlx"]
        assert omlx["state"] == "needs_setup" and omlx["fix"] == "Settings › Providers › Local model"

    def test_server_that_is_not_answering_needs_setup(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
        _server(status="offline")
        assert _sources(client)["omlx"]["state"] == "needs_setup"

    def test_online_server_without_models_needs_setup(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
        _server()
        assert _sources(client)["omlx"]["state"] == "needs_setup"

    def test_online_server_with_a_model_is_ready(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
        sid = _server()
        _model(f"omlx@{sid}/qwen")
        omlx = _sources(client)["omlx"]
        assert (omlx["state"], omlx["fix"], omlx["reason"]) == ("ready", None, None)

    def test_an_external_endpoint_never_counts_as_the_local_model(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
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
        from connections.source_state import apply_check

        _put("codex", "available")
        apply_check("codex", "ready")
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        harness_usage.record_usage(provider="codex", verdict={
            "state": "limited", "reason": "five_hour_limit", "resets_at": reset,
            "source": "discovery"}, receipt=default_receipt("codex"))
        codex = _sources(client)["codex"]
        assert codex["state"] == "ready" and codex["limited"] is True
        assert codex["resets_at"] == reset

    def test_unlimited_source_has_no_limited_key(self, isolated_db, client):
        from connections.source_state import apply_check

        _put("codex", "available")
        apply_check("codex", "ready")
        assert "limited" not in _sources(client)["codex"]


class TestRecheck:
    def test_requires_a_signed_in_caller(self, isolated_db):
        from api.provider_sources import router

        app = FastAPI()
        app.include_router(router)
        bare = TestClient(app)
        assert bare.get("/api/providers/sources").status_code == 401
        assert bare.post("/api/providers/sources/recheck").status_code == 401

    def test_local_setup_route_returns_the_current_background_stage(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("api.provider_sources.apply_foreign_omlx_choice", lambda **_kwargs: None)
        result = {"ok": True, "in_progress": True, "stage": "Downloading model (8.60 GiB)"}
        calls = []

        async def begin(**kwargs):
            calls.append(kwargs)
            return result

        monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
        assert client.post("/api/providers/local/setup").json() == result
        assert calls == [{"no_subscription_only": False, "automatic": False}]
        assert client.post("/api/providers/local/setup", json={
            "no_subscription_only": True, "automatic": True,
        }).json() == result
        assert calls[-1] == {"no_subscription_only": True, "automatic": True}

    def test_local_setup_route_reuses_keep_both_after_a_pid_change_and_excludes_unmanaged_rows(
            self, isolated_db, client, monkeypatch, tmp_path):
        from llm import local_setup_ownership, local_sources, network_discovery

        current = {"pid": 4242}
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_choice_path", lambda: tmp_path / "choice.json")
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_process", lambda: current["pid"])
        monkeypatch.setattr(local_setup_ownership, "stop_foreign_omlx_process", lambda pid: pytest.fail(f"must not stop {pid}"))
        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: None)
        foreign = network_discovery.upsert_server(
            "omlx", "foreign-omlx", "Foreign", "127.0.0.1", 8000, "online", kind="managed",
        )
        foreign_model = f"omlx@{foreign}/qwen"
        _model(foreign_model)

        async def begin(**_kwargs):
            return {"ok": True, "in_progress": True}

        monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
        assert client.post("/api/providers/local/setup", json={
            "consent_existing": True, "manage_existing": False,
        }).status_code == 200
        current["pid"] = 4243
        assert client.post("/api/providers/local/setup").status_code == 200
        assert _sources(client)["omlx"]["state"] == "not_installed"
        # A manually added loopback row remains routable across setup checks.
        from llm.local_setup_runtime import retire_unowned_local_omlx_rows

        assert retire_unowned_local_omlx_rows() == []
        assert f"omlx@{foreign}" in set().union(*local_sources.catalog_membership())

    def test_local_setup_route_explicit_stop_supersedes_remembered_keep_for_the_same_pid(
            self, isolated_db, client, monkeypatch, tmp_path):
        from llm import local_setup_ownership

        choice = tmp_path / "choice.json"
        choice.write_text('{"pid": 4242, "stop": false}', encoding="utf-8")
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_choice_path", lambda: choice)
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_process", lambda: 4242)
        stops = []
        monkeypatch.setattr(local_setup_ownership, "stop_foreign_omlx_process", lambda pid: stops.append(pid) or True)

        async def begin(**_kwargs):
            return {"ok": True}

        monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
        assert client.post("/api/providers/local/setup", json={
            "consent_existing": True, "manage_existing": True,
        }).status_code == 200
        assert stops == [4242]
        assert choice.read_text(encoding="utf-8") == '{"pid": 4242, "stop": true}'

    def test_local_setup_route_applies_a_preloaded_stop_choice_to_its_exact_pid_once(
            self, isolated_db, client, monkeypatch, tmp_path):
        from llm import local_setup_ownership

        current = {"pid": 4242}
        choice = tmp_path / "choice.json"
        choice.write_text('{"pid": 4242, "stop": true}', encoding="utf-8")
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_choice_path", lambda: choice)
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_process", lambda: current["pid"])
        stops = []

        def stop(pid):
            stops.append(pid)
            current["pid"] = None
            return True

        monkeypatch.setattr(local_setup_ownership, "stop_foreign_omlx_process", stop)

        async def begin(**_kwargs):
            return {"ok": True, "in_progress": True}

        monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
        assert client.post("/api/providers/local/setup").status_code == 200
        assert stops == [4242]

    def test_local_setup_route_never_applies_a_remembered_choice_to_a_changed_pid(
            self, isolated_db, client, monkeypatch, tmp_path):
        from llm import local_setup_ownership

        current = {"pid": 4242}
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_choice_path", lambda: tmp_path / "choice.json")
        monkeypatch.setattr(local_setup_ownership, "foreign_omlx_process", lambda: current["pid"])
        monkeypatch.setattr(local_setup_ownership, "stop_foreign_omlx_process", lambda _pid: True)

        async def begin(**_kwargs):
            return {"ok": True}

        monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
        assert client.post("/api/providers/local/setup", json={
            "consent_existing": True, "manage_existing": True,
        }).status_code == 200
        current["pid"] = 4243
        response = client.post("/api/providers/local/setup")
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "consent_required"}}

    def test_local_cancel_route_uses_the_canonical_flight_owner(self, isolated_db, client, monkeypatch):
        calls = []

        async def cancel():
            calls.append(True)
            return {"ok": True, "cancelled": True}

        monkeypatch.setattr("llm.local_setup.cancel_local_setup", cancel)
        assert client.post("/api/providers/local/cancel").json() == {"ok": True, "cancelled": True}
        assert calls == [True]

    def test_local_setup_route_requires_a_choice_for_an_identified_foreign_process(self, isolated_db, client, monkeypatch):
        monkeypatch.setattr("api.provider_sources.apply_foreign_omlx_choice", lambda **_kwargs: "consent_required")

        async def should_not_start(**_kwargs):
            raise AssertionError("setup must wait for a foreign-process choice")

        monkeypatch.setattr("llm.local_setup.begin_local_setup", should_not_start)
        response = client.post("/api/providers/local/setup")

        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "consent_required"}}

    @pytest.mark.parametrize(("body", "expected"), [
        ({"consent_existing": True, "manage_existing": False}, (True, False)),
        ({"consent_existing": True, "manage_existing": True}, (True, True)),
    ])
    def test_local_setup_route_applies_keep_or_stop_choice_once(self, isolated_db, client, monkeypatch, body, expected):
        choices = []
        monkeypatch.setattr(
            "api.provider_sources.apply_foreign_omlx_choice",
            lambda **kwargs: choices.append((kwargs["consent_existing"], kwargs["manage_existing"])) or None,
        )

        async def begin(**_kwargs):
            return {"ok": True, "in_progress": True}

        monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
        response = client.post("/api/providers/local/setup", json=body)

        assert response.status_code == 200
        assert choices == [expected]

    @pytest.mark.parametrize("refusal", ["foreign_omlx_changed", "consent_required"])
    def test_local_setup_route_does_not_start_when_the_foreign_process_changed_or_stop_failed(
            self, isolated_db, client, monkeypatch, refusal):
        monkeypatch.setattr("api.provider_sources.apply_foreign_omlx_choice", lambda **_kwargs: refusal)

        async def should_not_start(**_kwargs):
            raise AssertionError("setup must not start after a foreign-process refusal")

        monkeypatch.setattr("llm.local_setup.begin_local_setup", should_not_start)
        response = client.post("/api/providers/local/setup", json={"consent_existing": True, "manage_existing": True})

        assert response.status_code == 409
        assert response.json() == {"detail": {"code": refusal}}

    def test_local_setup_status_route_returns_the_flight_record(self, isolated_db, client, monkeypatch):
        import asyncio

        from llm import local_model, local_setup

        monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
        monkeypatch.setattr(local_setup, "local_memory_ok", lambda *_args: True)
        # Status is derived from the managed local server and flight record.
        not_checked = {"state": "not_checked", "model": None, "message": None}
        idle = client.get("/api/providers/local/setup").json()
        assert idle["state"] == "stopped" and idle["local_model"] == not_checked

        failure = {"ok": False, "message": "oMLX failed"}
        local_setup._flight.result = failure
        failed = client.get("/api/providers/local/setup").json()
        assert failed["state"] == "failed" and failed["local_model"] == not_checked

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

        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
        managed = _server(status="offline")
        external = _server(status="offline", provider="external", kind="external", name="Lab")
        parked = _server(status="disabled", provider="external", kind="external", name="Off")
        calls: list = []

        async def check(provider, *, rediscover=False):
            calls.append(("check", provider, rediscover))

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
            ("check", "claude-code", True), ("check", "codex", True),
            ("sweep", managed), ("test", external)], key=str)
        assert parked not in {c[1] for c in calls}
        by_source = {s["source"]: s for s in body["sources"]}
        assert by_source["omlx"]["state"] == "ready"
        assert all(set(s) >= _KEYS for s in body["sources"])
        assert [s["source"] for s in body["sources"][:3]] == ["claude-code", "codex", "omlx"]

    def test_one_unrecorded_check_fails_the_route(self, isolated_db, client, monkeypatch):
        from api import provider_sources

        async def broken(provider, *, rediscover=False):
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

        monkeypatch.setattr("llm.local_sources.installed_omlx", lambda: "/bin/omlx")
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
    def test_startup_checks_each_harness_once_beside_the_catalog_refresh(self, monkeypatch):
        import app_lifecycle
        import connections.harness_refresh as harness_refresh
        import connections.startup as startup

        calls: list = []

        async def check(provider, *, rediscover=False):
            calls.append(("check", provider, rediscover))

        async def refresh(*, reason):
            calls.append(("refresh", reason))
            return [{"ok": True}]

        monkeypatch.setattr(startup, "check_harness_source", check)
        monkeypatch.setattr(harness_refresh, "refresh_harness_catalogs", refresh)
        assert asyncio.run(app_lifecycle._startup_harness_discovery()) == [{"ok": True}]
        assert sorted(calls) == [("check", "claude-code", False), ("check", "codex", False),
                                 ("refresh", "startup")]

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
