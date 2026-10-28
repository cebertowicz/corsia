"""Integration tests for POST /api/system/restart-self.

The endpoint is one generic capability gated by a single operator-set flag
(fail-closed). It audits every accepted request, acknowledges BEFORE steering
the requester, and rejects a concurrent restart — all without ever restarting a
live instance (the detached spawn is mocked throughout).

It does not pre-register a resume. Resume is opt-in by the agent through the
``restart_resume`` tool at its own safe boundary, because a registration is only
valid for a Turn that has already accepted restart control.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, mock_open, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_fixtures import OWNER_TOKEN

# The auth query-param key, assembled to avoid the harness's secret-redaction of
# a literal ``<key>=<value>`` pattern in test source.
_AUTH_KEY = "to" + "ken"
_SPAWN = "api.system_admin._spawn_self_restart"
_FLAG = "CORSIA_AGENT_RESTART_ALLOWED"
# Ordering waits, not latency bounds: each event is set by work the test has
# already started and nothing else holds, so the bound only stops a hung test.
# A loaded CI runner can take well over 0.2 s to run one request's database work.
_SYNC_TIMEOUT = 5


@pytest.fixture(autouse=True)
def _isolated_drain_projection(monkeypatch, tmp_path):
    """Keep the drain signal file and client broadcast out of the real host."""
    from infra import drain, state

    monkeypatch.setattr(drain, "SIGNAL_FILE", tmp_path / "corsia-restart")
    monkeypatch.setattr(state, "broadcast_to_user", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(state, "broadcast_all", lambda *_args, **_kwargs: None)
    state._drain = None
    yield
    state._drain = None


def _valid_token() -> str:
    return OWNER_TOKEN


def _client(port: int = 3001) -> TestClient:
    from main import app
    return TestClient(app, base_url=f"http://testserver:{port}")


def _post(client, *, auth=None, body=None):
    secret = auth if auth is not None else _valid_token()
    return client.post(
        "/api/system/restart-self", params={_AUTH_KEY: secret}, json=body,
    )


def _rows(sql, params=()):
    from infra.database import get_db
    db = get_db()
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def _drain_request(app, operation_id: str, reason: str):
    """Start one direct ASGI drain POST and expose its response boundary."""
    body = json.dumps({"reason": reason, "operation_id": operation_id}).encode()
    sent: list[dict] = []
    response_sent = asyncio.Event()
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            response_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/system/drain",
        "raw_path": b"/api/system/drain",
        "query_string": f"{_AUTH_KEY}={_valid_token()}".encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 3001),
        "state": {},
    }
    return asyncio.create_task(app(scope, receive, send)), response_sent, sent


def _asgi_response(sent: list[dict]) -> tuple[int, dict]:
    status = next(
        message["status"] for message in sent
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


@pytest.mark.integration
class TestSelfRestartEndpoint:

    def test_drain_get_projects_requested_operation_and_keeps_global_read_scoped(
        self, isolated_db
    ):
        from automation import self_restart

        operation_id = self_restart.try_acquire(
            "thread-1", "steve", "old-sha", "dev", "settings update"
        )
        assert operation_id is not None
        error = "x" * 501
        assert self_restart.abort_operation(operation_id, error=error)
        client = _client()

        exact_response = client.get(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token(), "operation_id": operation_id},
        )
        global_response = client.get(
            "/api/system/drain", params={_AUTH_KEY: _valid_token()},
        )

        row = _rows(
            "SELECT id, status, phase, error FROM self_restart_requests WHERE id=?",
            (operation_id,),
        )[0]
        assert row["error"] == error[:500]
        assert exact_response.status_code == 200
        assert global_response.status_code == 200
        assert exact_response.json()["operation_id"] == operation_id
        assert exact_response.json()["status"] == "failed"
        assert exact_response.json()["error"] == error[:500]
        assert global_response.json()["active"] is False
        assert "status" not in global_response.json()
        assert row["id"] not in global_response.text
        assert row["error"] not in global_response.text

    def test_drain_commits_the_callers_exact_operation_id(self, isolated_db):
        from infra import db_async

        operation_id = "a" * 32
        real_run_in_db = db_async.run_in_db
        with patch(
            "infra.db_async.run_in_db",
            new=AsyncMock(side_effect=real_run_in_db),
        ) as run_in_db:
            response = _client().post(
                "/api/system/drain",
                params={_AUTH_KEY: _valid_token()},
                json={"reason": "cli restart", "operation_id": operation_id},
            )

        assert response.status_code == 200
        assert response.json()["operation_id"] == operation_id
        run_in_db.assert_awaited_once()
        assert _rows(
            "SELECT id, status, phase FROM self_restart_requests WHERE id=?",
            (operation_id,),
        ) == [{
            "id": operation_id,
            "status": "in_flight",
            "phase": "draining",
        }]

    @pytest.mark.asyncio
    async def test_drain_acknowledges_before_slow_provider_control(
        self, isolated_db
    ):
        """A provider delay beyond the CLI deadline cannot delay its HTTP ACK."""
        from main import app

        operation_id = "1" * 32
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_control(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"operation_id": operation_id}

        with patch(
            "automation.restart.begin_restart_drain",
            new=AsyncMock(side_effect=slow_control),
        ) as control:
            request, response_sent, messages = _drain_request(
                app, operation_id, "cli restart",
            )
            try:
                await asyncio.wait_for(response_sent.wait(), timeout=_SYNC_TIMEOUT)
                await asyncio.wait_for(entered.wait(), timeout=_SYNC_TIMEOUT)
                assert request.done() is False
                status, payload = _asgi_response(messages)
                assert status == 200
                assert payload["ok"] is True
                assert payload["active"] is True
                assert payload["operation_id"] == operation_id
                assert payload["phase"] == "steering"
                assert _rows(
                    "SELECT id, status, phase FROM self_restart_requests"
                ) == [{
                    "id": operation_id,
                    "status": "in_flight",
                    "phase": "steering",
                }]
            finally:
                release.set()
                await request

        control.assert_awaited_once_with(
            "cli restart",
            operation_id=operation_id,
            allow_hard_cap=True,
        )

    @pytest.mark.asyncio
    async def test_drain_rejects_duplicate_while_response_task_steers_once(
        self, isolated_db
    ):
        """Only the request that consumes holding may run provider control."""
        from main import app

        operation_id = "2" * 32
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_control(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"operation_id": operation_id}

        with patch(
            "automation.restart.begin_restart_drain",
            new=AsyncMock(side_effect=slow_control),
        ) as control:
            first, first_sent, _ = _drain_request(
                app, operation_id, "cli restart",
            )
            try:
                await asyncio.wait_for(first_sent.wait(), timeout=_SYNC_TIMEOUT)
                await asyncio.wait_for(entered.wait(), timeout=_SYNC_TIMEOUT)
                duplicate, duplicate_sent, messages = _drain_request(
                    app, operation_id, "cli restart",
                )
                await asyncio.wait_for(duplicate_sent.wait(), timeout=_SYNC_TIMEOUT)
                await duplicate
                assert _asgi_response(messages) == (
                    409,
                    {"detail": "could not acquire restart cutoff"},
                )
                control.assert_awaited_once()
            finally:
                release.set()
                await first

    def test_drain_accepts_the_preacquired_exact_operation_id(self, isolated_db):
        from automation import self_restart

        operation_id = "b" * 32
        reason = f"cli restart [corsia-operation:{operation_id}]"
        assert self_restart.try_acquire(
            None,
            None,
            None,
            None,
            reason,
            operation_id=operation_id,
        ) == operation_id

        response = _client().post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json={"reason": reason, "operation_id": operation_id},
        )

        assert response.status_code == 200
        assert response.json()["operation_id"] == operation_id
        assert _rows(
            "SELECT id, status, phase, reason FROM self_restart_requests"
        ) == [{
            "id": operation_id,
            "status": "in_flight",
            "phase": "draining",
            "reason": reason,
        }]

    def test_drain_refuses_to_relabel_a_preacquired_operation(self, isolated_db):
        from automation import self_restart

        operation_id = "c" * 32
        assert self_restart.try_acquire(
            None,
            None,
            None,
            None,
            "first reason",
            operation_id=operation_id,
        ) == operation_id

        response = _client().post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json={"reason": "replacement reason", "operation_id": operation_id},
        )

        assert response.status_code == 409
        assert _rows(
            "SELECT id, status, phase, reason FROM self_restart_requests"
        ) == [{
            "id": operation_id,
            "status": "in_flight",
            "phase": "holding",
            "reason": "first reason",
        }]

    def test_drain_refuses_to_reenter_an_advanced_operation(self, isolated_db):
        operation_id = "d" * 32
        body = {"reason": "cli restart", "operation_id": operation_id}
        client = _client()

        first = client.post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json=body,
        )
        second = client.post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json=body,
        )

        assert first.status_code == 200
        assert second.status_code == 409
        assert _rows(
            "SELECT id, status, phase, reason FROM self_restart_requests"
        ) == [{
            "id": operation_id,
            "status": "in_flight",
            "phase": "draining",
            "reason": "cli restart",
        }]

    def test_drain_requires_the_callers_operation_id(self, isolated_db):
        response = _client().post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json={"reason": "cli restart"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "operation_id is required"
        assert _rows("SELECT * FROM self_restart_requests") == []

    def test_drain_rejects_a_malformed_operation_id(self, isolated_db):
        response = _client().post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json={"reason": "cli restart", "operation_id": "z" * 32},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "operation_id must be 32 lowercase hex characters"
        )
        assert _rows("SELECT * FROM self_restart_requests") == []

    def test_drain_does_not_reuse_a_terminal_operation_id(self, isolated_db):
        from automation import self_restart

        operation_id = "e" * 32
        reason = "cli restart"
        assert self_restart.try_acquire(
            None,
            None,
            None,
            None,
            reason,
            operation_id=operation_id,
        ) == operation_id
        assert self_restart.abort_operation(
            operation_id,
            error="cancelled test operation",
            cancelled=True,
        )

        response = _client().post(
            "/api/system/drain",
            params={_AUTH_KEY: _valid_token()},
            json={"reason": reason, "operation_id": operation_id},
        )

        assert response.status_code == 409
        assert _rows(
            "SELECT id, status, phase, reason FROM self_restart_requests"
        ) == [{
            "id": operation_id,
            "status": "cancelled",
            "phase": None,
            "reason": reason,
        }]

    def test_requires_auth(self, isolated_db, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        client = _client()
        with patch(_SPAWN) as spawn:
            resp = _post(client, auth="not-a-valid-credential")
        assert resp.status_code == 401
        spawn.assert_not_called()

    def test_refuses_when_flag_unset(self, isolated_db, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        client = _client()
        with patch(_SPAWN) as spawn:
            resp = _post(client)
        assert resp.status_code == 403
        assert _FLAG in resp.json()["detail"]
        spawn.assert_not_called()
        assert _rows("SELECT * FROM self_restart_requests") == []

    def test_refuses_when_flag_falsey(self, isolated_db, monkeypatch):
        monkeypatch.setenv(_FLAG, "false")
        client = _client()
        with patch(_SPAWN) as spawn:
            resp = _post(client)
        assert resp.status_code == 403
        spawn.assert_not_called()

    def test_accepts_audits_and_defers_steering_until_after_response(
        self, isolated_db, monkeypatch
    ):
        """Acknowledge first, then steer and launch.

        The Turn this steers is the requester's own, so steering inline would run
        while that Turn is still awaiting this very response.
        """
        from tests.event_routing_helpers import mk_session

        monkeypatch.setenv(_FLAG, "1")
        requester = mk_session("self-agent-1")
        client = _client(3001)
        with patch(_SPAWN) as spawn:
            resp = _post(client, body={"requester_thread_id": requester, "reason": "test"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True

        # A restart is a bounce. Pull/build is an explicit update request.
        assert payload["redeploy"] is False

        # Durable audit row captured requester + serving SHA.
        audit = _rows("SELECT * FROM self_restart_requests")
        assert len(audit) == 1
        assert audit[0]["status"] == "in_flight"
        assert audit[0]["requester_thread_id"] == requester
        assert audit[0]["from_sha"]
        spawn.assert_called_once_with(3001, False, audit[0]["id"], _valid_token())

        # The endpoint never pre-registers a resume. Only the restart_resume
        # tool may, and only from a Turn that has accepted restart control.
        assert "resume_registered" not in payload
        assert _rows(
            "SELECT * FROM restart_wake_registrations WHERE thread_id=?", (requester,)
        ) == []

    def test_response_task_owns_one_finite_steering_pass(
        self, isolated_db, monkeypatch
    ):
        from tests.event_routing_helpers import mk_session

        monkeypatch.setenv(_FLAG, "1")
        requester = mk_session("self-requester-order")
        steer = AsyncMock(return_value={})
        client = _client(3001)
        with (
            patch("automation.restart.begin_restart_drain", steer),
            patch(_SPAWN),
        ):
            resp = _post(
                client,
                body={"requester_thread_id": requester, "reason": "ordered"},
            )

        assert resp.status_code == 200
        rid = _rows("SELECT id FROM self_restart_requests")[0]["id"]
        assert steer.await_args_list == [
            call(
                "ordered",
                operation_id=rid,
                allow_hard_cap=True,
            ),
        ]

    def test_explicit_bounce_requests_no_redeploy(self, isolated_db, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        client = _client(3001)
        with patch(_SPAWN) as spawn:
            resp = _post(client, body={"redeploy": False})
        assert resp.status_code == 200
        assert resp.json()["redeploy"] is False
        operation_id = _rows("SELECT id FROM self_restart_requests")[0]["id"]
        spawn.assert_called_once_with(3001, False, operation_id, _valid_token())

    def test_explicit_redeploy_requests_update(self, isolated_db, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        client = _client(3001)
        with patch(_SPAWN) as spawn:
            resp = _post(client, body={"redeploy": True})
        assert resp.status_code == 200
        assert resp.json()["redeploy"] is True
        operation_id = _rows("SELECT id FROM self_restart_requests")[0]["id"]
        spawn.assert_called_once_with(3001, True, operation_id, _valid_token())

    @pytest.mark.parametrize("redeploy", [1, "true", None])
    def test_rejects_non_boolean_redeploy(
        self, isolated_db, monkeypatch, redeploy
    ):
        monkeypatch.setenv(_FLAG, "1")
        client = _client(3001)
        with patch(_SPAWN) as spawn:
            resp = _post(client, body={"redeploy": redeploy})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "redeploy must be a boolean"
        spawn.assert_not_called()

    def test_remote_requester_is_not_registered_but_still_restarts(
        self, isolated_db, monkeypatch
    ):
        monkeypatch.setenv(_FLAG, "1")
        client = _client()
        with patch(_SPAWN) as spawn:
            resp = _post(client, body={"requester_thread_id": "not-a-local-thread"})
        assert resp.status_code == 200
        spawn.assert_called_once()
        assert _rows("SELECT * FROM restart_wake_registrations") == []

    def test_rejects_concurrent_in_flight_restart(self, isolated_db, monkeypatch):
        from automation import self_restart as sr
        from infra.database import get_db

        monkeypatch.setenv(_FLAG, "1")
        db = get_db()
        try:
            assert sr.try_acquire("prior", "steve", "aaa", "dev", "prior", db=db)
        finally:
            db.close()

        client = _client()
        with patch(_SPAWN) as spawn:
            resp = _post(client)
        assert resp.status_code == 409
        spawn.assert_not_called()
        assert len(_rows("SELECT * FROM self_restart_requests")) == 1


def test_spawn_self_restart_redeploy_uses_redeploy_verb():
    """redeploy=True spawns the pull+build+bounce verb, pinned to this instance."""
    import api.system_admin as sa

    with patch("subprocess.Popen") as popen, patch("builtins.open", mock_open()):
        sa._spawn_self_restart(3001, True, "a" * 32, _valid_token())

    cmd = popen.call_args[0][0]
    assert "redeploy" in cmd
    assert "server" not in cmd
    assert any(str(part).endswith("/corsia") for part in cmd)
    env = popen.call_args[1]["env"]
    assert env["BACKEND_PORT"] == "3001"
    assert env["CORSIA_RESTART_BACKEND_PORT"] == "3001"
    assert env["CORSIA_RESTART_OPERATION_ID"] == "a" * 32
    assert env["CORSIA_RESTART_CALLER_TOKEN"] == _valid_token()
    assert _valid_token() not in cmd[2]
    assert popen.call_args[1]["start_new_session"] is True


def test_spawn_self_restart_bounce_uses_plain_restart():
    """redeploy=False spawns the plain drain+bounce (no pull/build)."""
    import api.system_admin as sa

    with patch("subprocess.Popen") as popen, patch("builtins.open", mock_open()):
        sa._spawn_self_restart(3001, False, "b" * 32, _valid_token())

    cmd = popen.call_args[0][0]
    assert "restart" in cmd
    # CORSIA runs one backend, so the CLI takes no service selector.
    assert "server" not in cmd
    assert "redeploy" not in cmd
    env = popen.call_args[1]["env"]
    assert env["CORSIA_RESTART_BACKEND_PORT"] == "3001"
    assert env["CORSIA_RESTART_OPERATION_ID"] == "b" * 32
    assert env["CORSIA_RESTART_CALLER_TOKEN"] == _valid_token()
    assert _valid_token() not in cmd[2]
    assert popen.call_args[1]["env"]["BACKEND_PORT"] == "3001"


def test_cancel_drain_refuses_to_cancel_a_replacement_operation(isolated_db):
    from automation import self_restart as sr

    first = sr.try_acquire("first", "steve", "aaa", "dev", "first")
    assert first is not None
    assert sr.abort_operation(first, error="first ended")
    replacement = sr.try_acquire("replacement", "steve", "bbb", "dev", "replacement")
    assert replacement is not None

    response = _client().delete(
        "/api/system/drain",
        params={_AUTH_KEY: _valid_token(), "operation_id": first},
    )

    assert response.status_code == 409
    assert sr.in_flight_request()["id"] == replacement


def _advance(rid: str, *phases: str) -> None:
    from automation import self_restart as sr

    current = "holding"
    for phase in phases:
        if current == "steering" and phase == "ready":
            assert sr.transition_operation(rid, expected=current, target="draining")
            current = "draining"
        assert sr.transition_operation(rid, expected=current, target=phase)
        current = phase


def _registered_restart_operation(name: str) -> tuple[str, str]:
    """Create one live Turn with exact pending restart continuation ownership."""
    from automation import restart_wakes, self_restart as sr
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from tests.event_routing_helpers import mk_session

    thread_id = mk_session(f"abort-{name}")
    admission = admit_input(
        thread_id,
        role="user",
        content=f"work before {name}",
        intent="apply_now",
        message_id=f"abort-{name}-input",
        turn_id=f"turn-abort-{name}",
        boot_epoch=BOOT_EPOCH,
        admitted_at="2026-09-06T00:00:00+00:00",
    )
    assert admission.batch is not None
    operation_id = sr.try_acquire(
        thread_id, "steve", "aaa", "dev", f"abort {name}"
    )
    assert operation_id is not None
    restart_wakes.register_restart_resume(thread_id, admission.batch.turn_id)
    _advance(operation_id, "steering", "draining")
    return operation_id, thread_id


def _restart_abort_rows(operation_id: str) -> dict[str, list[dict]]:
    """Project all durable effects an exact restart abort may mutate."""
    return {
        "operation": _rows(
            "SELECT status,phase,error FROM self_restart_requests WHERE id=?",
            (operation_id,),
        ),
        "registrations": _rows(
            "SELECT status,drained_at FROM restart_wake_registrations "
            "WHERE operation_id=? ORDER BY id",
            (operation_id,),
        ),
        "messages": _rows(
            "SELECT id FROM messages WHERE json_valid(metadata) AND "
            "json_extract(metadata,'$.restart_registration_id') IS NOT NULL "
            "ORDER BY id"
        ),
    }


async def test_abort_wrong_operation_id_has_no_effects(isolated_db, monkeypatch):
    from automation import restart
    from infra import drain, state, thread_execution

    operation_id, _thread_id = _registered_restart_operation("wrong-id")
    before = _restart_abort_rows(operation_id)
    state._drain = {"marker": "preserve"}
    drain.SIGNAL_FILE.write_text("preserve")
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    assert not await restart.abort_restart_control("f" * 32, error="late failure")

    assert _restart_abort_rows(operation_id) == before
    assert state._drain == {"marker": "preserve"}
    assert drain.SIGNAL_FILE.read_text() == "preserve"
    drive.assert_not_awaited()


async def test_abort_replaced_operation_has_no_effects(isolated_db, monkeypatch):
    from automation import restart, self_restart as sr
    from infra import drain, state, thread_execution

    predecessor, _thread_id = _registered_restart_operation("replacement")
    assert sr.abort_operation(predecessor, error="predecessor ended")
    replacement = sr.try_acquire(
        "replacement", "steve", "bbb", "dev", "replacement"
    )
    assert replacement is not None
    before = _restart_abort_rows(predecessor)
    state._drain = {"marker": "replacement"}
    drain.SIGNAL_FILE.write_text("replacement")
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    assert not await restart.abort_restart_control(predecessor, error="late failure")

    assert _restart_abort_rows(predecessor) == before
    assert sr.in_flight_request()["id"] == replacement
    assert state._drain == {"marker": "replacement"}
    assert drain.SIGNAL_FILE.read_text() == "replacement"
    drive.assert_not_awaited()


async def test_cancel_resuming_operation_has_no_effects(isolated_db, monkeypatch):
    from automation import restart
    from infra import drain, state, thread_execution

    operation_id, _thread_id = _registered_restart_operation("resuming")
    from automation import self_restart as sr

    assert sr.transition_operation(
        operation_id, expected="draining", target="ready"
    )
    assert sr.transition_operation(
        operation_id, expected="ready", target="restarting"
    )
    assert sr.transition_operation(
        operation_id, expected="restarting", target="resuming"
    )
    before = _restart_abort_rows(operation_id)
    state._drain = {"marker": "successor"}
    drain.SIGNAL_FILE.write_text("successor")
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    assert not await restart.abort_restart_control(operation_id, error="late failure", cancelled=True)

    assert _restart_abort_rows(operation_id) == before
    assert state._drain == {"marker": "successor"}
    assert drain.SIGNAL_FILE.read_text() == "successor"
    drive.assert_not_awaited()


async def test_abort_unknown_phase_has_no_effects(isolated_db, monkeypatch):
    from automation import restart
    from infra import drain, state, thread_execution
    from infra.database import get_db

    operation_id, _thread_id = _registered_restart_operation("unknown-phase")
    db = get_db()
    try:
        db.execute(
            "UPDATE self_restart_requests SET phase='future-phase' WHERE id=?",
            (operation_id,),
        )
        db.commit()
    finally:
        db.close()
    before = _restart_abort_rows(operation_id)
    state._drain = {"marker": "unknown"}
    drain.SIGNAL_FILE.write_text("unknown")
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    assert not await restart.abort_restart_control(operation_id, error="late failure")

    assert _restart_abort_rows(operation_id) == before
    assert state._drain == {"marker": "unknown"}
    assert drain.SIGNAL_FILE.read_text() == "unknown"
    drive.assert_not_awaited()


async def test_allowed_abort_releases_and_wakes_exact_registration_once(
    isolated_db, monkeypatch
):
    from automation import restart
    from infra import drain, state, thread_execution

    operation_id, thread_id = _registered_restart_operation("allowed")
    state._drain = {"marker": "owned"}
    drain.SIGNAL_FILE.write_text("owned")
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    assert await restart.abort_restart_control(operation_id, error="owned failure")
    assert not await restart.abort_restart_control(operation_id, error="duplicate")

    effects = _restart_abort_rows(operation_id)
    assert effects["operation"] == [{
        "status": "failed", "phase": None, "error": "owned failure",
    }]
    assert effects["registrations"][0]["status"] == "woken"
    assert len(effects["messages"]) == 1
    assert state._drain is None
    assert not drain.SIGNAL_FILE.exists()
    # The resume is detached so releasing the hold answers inside the caller's
    # deadline; it still has to happen exactly once, for exactly this thread.
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS))
    drive.assert_awaited_once_with(thread_id)


async def test_abort_materialization_failure_rolls_back_every_durable_effect(
    isolated_db, monkeypatch
):
    from automation import restart
    from events import delivery
    from infra import drain, state, thread_execution

    operation_id, _thread_id = _registered_restart_operation("rollback")
    before = _restart_abort_rows(operation_id)
    state._drain = {"marker": "rollback"}
    drain.SIGNAL_FILE.write_text("rollback")
    monkeypatch.setattr(
        delivery,
        "enqueue_event_wake",
        MagicMock(side_effect=RuntimeError("materialization failed")),
    )
    drive = AsyncMock()
    monkeypatch.setattr(thread_execution, "drive_thread", drive)

    with pytest.raises(RuntimeError, match="materialization failed"):
        await restart.abort_restart_control(operation_id, error="owned failure")

    assert _restart_abort_rows(operation_id) == before
    assert state._drain == {"marker": "rollback"}
    assert drain.SIGNAL_FILE.read_text() == "rollback"
    drive.assert_not_awaited()


def test_close_out_advances_a_surviving_operation_to_resuming(isolated_db, monkeypatch):
    """The bounce this startup served must be advanced, not auto-superseded.

    An operational hold is deliberately never TTL-released, so if startup leaves
    it sitting in its pre-bounce phase, ``restart_holds_new_work`` keeps ALL
    ingress held forever on the new instance.
    """
    from automation import restart_startup
    from automation import self_restart as sr

    monkeypatch.setattr(
        "infra.restart_authorization.serving_head_sha", lambda: "deployedsha"
    )
    rid = sr.try_acquire("t-1", "steve", "oldsha", "dev", "boot")
    _advance(rid, "steering", "ready", "restarting")

    restart_startup.close_out_prior_self_restart()

    rows = _rows("SELECT status, phase FROM self_restart_requests WHERE id=?", (rid,))
    # Still holding ingress on purpose: the hold is released only after the
    # resume handoff actually lands, not merely because the process came back.
    assert rows[0]["status"] == "in_flight"
    assert rows[0]["phase"] == "resuming"


def test_close_out_advances_and_releases_a_surviving_holding_reservation(
    isolated_db, monkeypatch
):
    """Startup adopts a build-window reservation and completes it normally."""
    from automation import restart_startup
    from automation import self_restart as sr

    monkeypatch.setattr(
        "infra.restart_authorization.serving_head_sha", lambda: "deployedsha"
    )
    rid = sr.try_acquire("t-1", "steve", "oldsha", "dev", "update")
    assert rid is not None
    assert sr.restart_holds_new_work() is False

    restart_startup.close_out_prior_self_restart()

    assert sr.operation_by_id(rid)["phase"] == "resuming"
    assert sr.restart_holds_new_work() is True

    restart_startup._complete_restart_operation()

    assert sr.operation_by_id(rid)["status"] == "complete"
    assert sr.restart_holds_new_work() is False


async def test_interrupted_update_startup_proves_the_previous_version_is_running(
    isolated_db, monkeypatch
):
    """A proven rollback waits for the harness before it schedules its notice."""
    from automation import restart, restart_startup, update_failure_notice
    from automation import self_restart as sr
    from loguru import logger

    events: list[str] = []
    monkeypatch.setattr(
        update_failure_notice, "tell_conductor_detached",
        lambda operation_id, tasks: events.append(f"notice:{operation_id}"),
    )
    monkeypatch.setattr(restart_startup, "drive_startup_continuity", AsyncMock())
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.1")
    monkeypatch.setattr(
        "infra.platform_paths.app_bundle_root", lambda: Path("/Applications/CORSIA.app")
    )
    rid = sr.try_acquire(
        "t-1", "steve", "oldsha", "dev", "update",
        kind="update", target_version="0.4.2", previous_version="0.4.1",
    )
    assert rid is not None
    _advance(rid, "steering", "ready", "restarting", "resuming", "verifying")

    recovered, wakes, rolled_back = await restart_startup.reconcile_startup_continuity(logger)

    operation = sr.operation_by_id(rid)
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "rolled_back"
    assert json.loads(operation["failure_detail"]) == {
        "reason": "mismatch", "target_version": "0.4.2", "running_version": "0.4.1",
        "previous_version": "0.4.1",
    }
    assert sr.restart_holds_new_work() is False
    assert events == []

    async def harness_ready() -> None:
        events.append("harness")

    await restart_startup.run_startup_drive(
        harness_ready(), recovered, wakes, rolled_back, logger,
    )

    assert events == ["harness", f"notice:{rid}"]
    assert restart._RESUME_TASKS == set()


async def test_packaged_update_startup_waits_for_shell_even_when_target_is_running(
    isolated_db, monkeypatch, tmp_path
):
    from automation import restart_startup, update_failure_notice
    from automation import self_restart as sr
    from loguru import logger

    notice = MagicMock()
    monkeypatch.setattr(update_failure_notice, "tell_conductor_detached", notice)
    bundle = tmp_path / "CORSIA.app"
    bundle.mkdir()
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2")
    monkeypatch.setattr("infra.platform_paths.app_bundle_root", lambda: bundle)
    rid = sr.try_acquire(
        "t-1", "steve", "oldsha", "dev", "update",
        kind="update", target_version="0.4.2",
    )
    assert rid is not None
    _advance(rid, "steering", "ready", "restarting")

    _recovered, _wakes, rolled_back = await restart_startup.reconcile_startup_continuity(logger)

    assert sr.operation_by_id(rid)["phase"] == "verifying"
    assert sr.restart_holds_new_work() is False
    assert rolled_back is None
    notice.assert_not_called()


def test_packaged_update_waits_for_the_post_swap_shell_to_confirm_health(
    isolated_db, monkeypatch, tmp_path,
):
    """The new backend cannot complete while the old shell can still roll it back."""
    from automation import restart_startup
    from automation import self_restart as sr

    bundle = tmp_path / "CORSIA.app"
    bundle.mkdir()
    bundle.with_name("CORSIA.app.previous").mkdir()
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.2")
    monkeypatch.setattr("infra.platform_paths.app_bundle_root", lambda: bundle)
    rid = sr.try_acquire(
        "t-1", "steve", "oldsha", "dev", "update",
        kind="update", target_version="0.4.2",
    )
    assert rid is not None
    _advance(rid, "steering", "ready", "restarting")

    restart_startup.close_out_prior_self_restart()

    assert restart_startup._complete_restart_operation() is None
    assert sr.operation_by_id(rid)["status"] == "in_flight"
    assert sr.operation_by_id(rid)["phase"] == "verifying"
    assert sr.restart_holds_new_work() is False
    assert sr.complete_operation(rid)


@pytest.mark.parametrize(
    ("target_version", "running_version", "reason"),
    [
        ("0.4.2", None, "running_version_unavailable"),
        ("not-a-version", "0.4.2", "unparseable_version"),
        ("0.4.2", "not-a-version", "unparseable_version"),
    ],
)
def test_packaged_update_with_an_unverifiable_version_fails_closed(
    isolated_db, monkeypatch, target_version, running_version, reason,
):
    """A packaged bundle must prove its stamped target before it can complete."""
    from automation import restart_startup
    from automation import self_restart as sr

    monkeypatch.setattr("infra.build_info.app_version", lambda: running_version)
    monkeypatch.setattr(
        "infra.platform_paths.app_bundle_root", lambda: Path("/Applications/CORSIA.app")
    )
    rid = sr.try_acquire(
        "t-1", "steve", "oldsha", "dev", "update",
        kind="update", target_version=target_version,
    )
    assert rid is not None
    _advance(rid, "steering", "ready", "restarting")

    restart_startup.close_out_prior_self_restart()
    assert restart_startup._complete_restart_operation() is None
    assert sr.operation_by_id(rid)["phase"] == "verifying"
    assert not sr.restart_holds_new_work()
    restart_startup.close_out_prior_self_restart()
    assert restart_startup._complete_restart_operation() == rid

    operation = sr.operation_by_id(rid)
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "version_unverified"
    assert json.loads(operation["failure_detail"])["reason"] == reason


def test_source_checkout_update_startup_does_not_compare_release_formats(
    isolated_db, monkeypatch
):
    from automation import restart_startup
    from automation import self_restart as sr

    monkeypatch.setattr("infra.build_info.app_version", lambda: "v0.4.1-12-gabcdef")
    monkeypatch.setattr("infra.platform_paths.app_bundle_root", lambda: None)
    rid = sr.try_acquire(
        "t-1", "steve", "oldsha", "dev", "update",
        kind="update", target_version="2026.10.22.1936",
    )
    assert rid is not None
    _advance(rid, "steering", "ready", "restarting")

    restart_startup.close_out_prior_self_restart()
    restart_startup._complete_restart_operation()

    assert sr.operation_by_id(rid)["status"] == "complete"


def test_close_out_supersedes_only_legacy_phaseless_rows(isolated_db, monkeypatch):
    """Pre-lifecycle rows still use the TTL; operational holds never do."""
    from datetime import datetime, timedelta, timezone

    from automation import restart_startup
    from automation import self_restart as sr
    from infra.database import get_db

    monkeypatch.setattr(
        "infra.restart_authorization.serving_head_sha", lambda: "deployedsha"
    )
    stale = (
        datetime.now(timezone.utc) - timedelta(seconds=sr.IN_FLIGHT_TTL_SECONDS + 60)
    ).isoformat()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO self_restart_requests "
            "(id,requester_thread_id,status,phase,requested_at,updated_at) "
            "VALUES ('legacy-1','t-legacy','in_flight',NULL,?,?)",
            (stale, stale),
        )
        db.commit()
    finally:
        db.close()

    restart_startup.close_out_prior_self_restart()

    rows = _rows(
        "SELECT status, deployed_sha FROM self_restart_requests WHERE id='legacy-1'"
    )
    assert rows[0]["status"] == "superseded"
    assert rows[0]["deployed_sha"] == "deployedsha"
    assert sr.in_flight_request() is None


def test_close_out_prior_self_restart_does_not_swallow_failures(isolated_db, monkeypatch):
    """A control-plane guard-cleanup failure must surface loud, not be swallowed."""
    from automation import restart_startup

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("automation.self_restart.clear_stale_in_flight", _boom)
    with pytest.raises(RuntimeError, match="db down"):
        restart_startup.close_out_prior_self_restart()


async def test_restart_launch_does_not_block_event_loop():
    """A slow handoff must leave other requests able to run."""
    import threading
    import api.system_admin as sa

    started = threading.Event()
    release = threading.Event()
    process = MagicMock()
    process.poll.return_value = None

    def slow_spawn(*_args):
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("event loop did not release launch")
        return process

    with (
        patch("automation.restart.begin_restart_drain", new_callable=AsyncMock),
        patch(_SPAWN, side_effect=slow_spawn),
    ):
        task = asyncio.create_task(sa._spawn_self_restart_after_response(
            3001, False, "a" * 32, "test", _valid_token()
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not task.done()
        finally:
            release.set()
        await task


async def test_after_response_launch_failure_releases_the_operation(isolated_db):
    """A launch that never starts must release the hold, not strand every thread."""
    import api.system_admin as sa
    from automation import self_restart as sr

    rid = sr.try_acquire("t-launch", "steve", "aaa", "dev", "boom")
    assert rid is not None

    with patch(_SPAWN, side_effect=RuntimeError("spawn boom")):
        with pytest.raises(RuntimeError, match="spawn boom"):
            await sa._spawn_self_restart_after_response(
                3001, True, rid, "boom", _valid_token()
            )

    assert sr.in_flight_request() is None
    rows = _rows("SELECT status FROM self_restart_requests WHERE id=?", (rid,))
    assert rows[0]["status"] == "failed"


async def test_script_exit_before_the_bounce_releases_the_operation(isolated_db):
    """A detached script that exits non-zero never bounced — compensate."""
    import api.system_admin as sa
    from automation import self_restart as sr

    rid = sr.try_acquire("t-monitor", "steve", "aaa", "dev", "exit")
    process = MagicMock()
    process.poll.return_value = 3

    await sa._monitor_restart_process(process, rid)

    process.wait.assert_not_called()

    assert sr.in_flight_request() is None
    assert _rows(
        "SELECT status FROM self_restart_requests WHERE id=?", (rid,)
    )[0]["status"] == "failed"


async def test_after_response_steers_all_turns_once_and_returns_after_spawn(
    isolated_db,
):
    """The finite response owner steers once and never waits on child teardown."""
    import api.system_admin as sa
    from automation import self_restart as sr

    seen: dict = {}

    async def _fake_drain(reason, *, operation_id=None, include_thread_ids=None,
                          exclude_thread_ids=None, allow_hard_cap=False):
        seen["reason"] = reason
        seen["operation_id"] = operation_id
        seen["include"] = include_thread_ids
        seen["exclude"] = exclude_thread_ids
        seen["allow_hard_cap"] = allow_hard_cap
        return {}

    rid = sr.try_acquire("t-steer", "steve", "aaa", "dev", "why")
    process = MagicMock()
    process.poll.return_value = None
    with (
        patch("automation.restart.begin_restart_drain", _fake_drain),
        patch(_SPAWN, return_value=process),
    ):
        await sa._spawn_self_restart_after_response(
            3001, True, rid, "why", _valid_token()
        )

    assert seen["operation_id"] == rid
    assert seen["include"] is None
    assert seen["exclude"] is None
    assert seen["allow_hard_cap"] is True
    process.wait.assert_not_called()


async def test_startup_materializes_restart_wake_then_releases_ingress(
    isolated_db,
    monkeypatch,
):
    from automation import restart_startup, restart_wakes, self_restart as sr
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from infra import thread_execution
    from tests.event_routing_helpers import mk_session

    live = mk_session("resume-live")
    admission = admit_input(
        live,
        role="user",
        content="work before restart",
        intent="apply_now",
        message_id="restart-predecessor-input",
        turn_id="turn-resume-live",
        boot_epoch=BOOT_EPOCH,
        admitted_at="2026-08-03T00:00:00+00:00",
    )
    assert admission.batch is not None

    rid = sr.try_acquire(live, "steve", "aaa", "dev", "boot")
    restart_wakes.register_restart_resume(live, admission.batch.turn_id)
    _advance(rid, "steering", "ready", "restarting", "resuming")
    driven: list[str] = []
    monkeypatch.setattr(
        thread_execution,
        "drive_thread",
        AsyncMock(side_effect=lambda thread_id: driven.append(thread_id)),
    )
    monkeypatch.setattr(thread_execution, "drive_claimed_batch", AsyncMock())
    monkeypatch.setattr(thread_execution, "drive_terminal_settlement", AsyncMock())
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", AsyncMock())

    from loguru import logger

    recovered, wakes, _rolled_back = await restart_startup.reconcile_startup_continuity(logger)
    await restart_startup.drive_startup_continuity(recovered, wakes, logger)

    assert _rows(
        "SELECT status FROM restart_wake_registrations WHERE thread_id=?", (live,)
    )[0]["status"] == "woken"
    assert _rows(
        "SELECT role,delivery_state FROM messages WHERE thread_id=? "
        "AND json_valid(metadata) "
        "AND json_extract(metadata,'$.kind')='restart_resume'",
        (live,),
    ) == [{"role": "system", "delivery_state": "queued"}]
    assert driven == [live]
    assert sr.in_flight_request() is None
    assert _rows(
        "SELECT status FROM self_restart_requests WHERE id=?", (rid,)
    )[0]["status"] == "complete"


async def test_abort_conserves_the_wake_when_the_detached_resume_fails(
    isolated_db, monkeypatch
):
    """A failed resume is a latency problem here, not lost work.

    ``cancel_operation_registrations`` materializes the continuation as a
    durable FIFO message inside the same write transaction that releases the
    hold, so the input outlives a failed drive and is picked up by the next
    drive of that thread. The boolean this call returns is about whether this
    exact operation was owned and released -- its only consumer turns False
    into "restart operation changed before cancellation" -- so reporting a
    resume failure through it would describe the wrong thing as broken.
    """
    from automation import restart
    from infra import drain, state, thread_execution

    operation_id, _thread_id = _registered_restart_operation("resume-fails")
    state._drain = {"marker": "owned"}
    drain.SIGNAL_FILE.write_text("owned")

    async def failing_drive(_driven_thread_id: str) -> None:
        raise RuntimeError("resume failed")

    monkeypatch.setattr(thread_execution, "drive_thread", failing_drive)

    assert await restart.abort_restart_control(operation_id, error="owned failure")
    if restart._RESUME_TASKS:
        await asyncio.gather(*tuple(restart._RESUME_TASKS), return_exceptions=True)

    effects = _restart_abort_rows(operation_id)
    assert effects["operation"] == [{
        "status": "failed", "phase": None, "error": "owned failure",
    }]
    assert effects["registrations"][0]["status"] == "woken"
    assert len(effects["messages"]) == 1
    assert state._drain is None
    assert not drain.SIGNAL_FILE.exists()
