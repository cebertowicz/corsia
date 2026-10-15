"""Assistant Google sign-in adoption stays CA3-safe across every handover branch."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from connections.google import handover, oauth
from infra import key_store
from infra.conductor import set_conductor
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.google_account import GoogleAdoptAssistantSignInTool

REFRESH = "1//handover-refresh-canary"
SECRET = "handover-client-secret-canary"
CLIENT_ID = "handover-client.apps.googleusercontent.com"
EMAIL = "person@example.com"
_REAL_CLIENT = httpx.AsyncClient


class FakeGoogle:
    def __init__(self) -> None:
        self.challenge = ""
        self.denied = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == oauth.TOKEN_URL:
            if self.denied:
                return httpx.Response(400, json={"error": "access_denied"})
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            assert form["grant_type"] == "authorization_code"
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenge
            return httpx.Response(200, json={
                "access_token": "handover-access-canary",
                "refresh_token": REFRESH,
                "scope": " ".join(oauth.SCOPES),
            })
        if str(request.url) == oauth.PROFILE_URL:
            return httpx.Response(200, json={"emailAddress": EMAIL})
        return httpx.Response(404)


@pytest.fixture
def handover_env(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    homes = tmp_path / "gws-homes"
    monkeypatch.setenv("CORSIA_GWS_AGENT_HOMES_DIR", str(homes))
    fake = FakeGoogle()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr("connections.google.client.httpx.AsyncClient", factory)
    monkeypatch.setattr(oauth, "_pending", None)
    return fake, homes


@pytest.fixture
def legacy_assistant(handover_env):
    _, homes = handover_env
    db = get_db()
    conductor = make_session(db, prefix="conductor")
    assert set_conductor("workspace", conductor, db=db)
    assistant = make_session(db, prefix="assistant", title="Assistant")
    db.execute(
        "UPDATE threads SET agent_enabled=1,agent_responsibility=?,agent_domains=?,agent_notes=? WHERE id=?",
        (
            handover.LEGACY_ASSISTANT_RESPONSIBILITY,
            json.dumps(["email", "calendar", "drive", "docs"]),
            handover.LEGACY_ASSISTANT_NOTES,
            assistant,
        ),
    )
    db.execute(
        "INSERT INTO automation_schedules "
        "(id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,payload_json,created_by_actor,created_at,updated_at) "
        "VALUES ('check',?, 'workspace','cadence','3600','2099-01-01T00:00:00+00:00',1000,'active',?,'assistant','now','now')",
        (assistant, json.dumps({"kind": "assistant.scheduled_email_check"})),
    )
    db.execute(
        "INSERT INTO automation_schedules "
        "(id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,payload_json,created_by_actor,created_at,updated_at) "
        "VALUES ('other',?, 'workspace','cadence','86400','2099-01-01T00:00:00+00:00',1000,'paused',?,'person','now','now')",
        (assistant, json.dumps({"kind": "person.weekly_reminder"})),
    )
    db.commit()
    client = homes / assistant / "config" / "desktop-client.json"
    client.parent.mkdir(parents=True)
    client.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": SECRET}}), encoding="utf-8")
    return conductor, assistant, client


def _pending_state(url: str, fake: FakeGoogle) -> str:
    query = parse_qs(urlparse(url).query)
    fake.challenge = query["code_challenge"][0]
    return query["state"][0]


async def _start(legacy_assistant, handover_env) -> str:
    conductor, _, _ = legacy_assistant
    fake, _ = handover_env
    url = await handover.start(conductor)
    return _pending_state(url, fake)


def _legacy_state(assistant: str, client: Path) -> tuple[str, str, bool]:
    db = get_db()
    thread = db.execute("SELECT lifecycle_state FROM threads WHERE id=?", (assistant,)).fetchone()[0]
    # Both the email check and the person's own schedule must move together.
    states = {
        row[0]: row[1]
        for row in db.execute("SELECT id,state FROM automation_schedules WHERE id IN ('check','other')")
    }
    if states == {"check": "active", "other": "paused"}:
        schedule = "active"
    elif set(states.values()) == {"cancelled"} and len(states) == 2:
        schedule = "cancelled"
    else:
        schedule = f"mixed:{states}"
    return thread or "active", schedule, client.exists()


@pytest.mark.asyncio
async def test_success_cancels_archives_removes_home_then_saves_google(legacy_assistant, handover_env):
    _, assistant, client = legacy_assistant
    state = await _start(legacy_assistant, handover_env)

    assert key_store.resolve_google() is None
    assert await oauth.complete_sign_in("code", state) == EMAIL

    assert _legacy_state(assistant, client) == ("archived", "cancelled", False)
    assert key_store.resolve_google() is not None
    assert oauth.account_status()["status"] == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,denied", [(None, False), ("code", True)], ids=["cancelled", "denied"])
async def test_cancelled_or_denied_oauth_leaves_legacy_assistant_intact(
    legacy_assistant, handover_env, code, denied,
):
    _, assistant, client = legacy_assistant
    fake, _ = handover_env
    state = await _start(legacy_assistant, handover_env)
    fake.denied = denied

    with pytest.raises(oauth.GoogleSignInError):
        await oauth.complete_sign_in(code, state)

    assert _legacy_state(assistant, client) == ("active", "active", True)
    assert key_store.resolve_google() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_step", ["schedule", "archive", "home"])
async def test_cleanup_failure_writes_no_token_and_retry_finishes(
    legacy_assistant, handover_env, monkeypatch, failing_step,
):
    conductor, assistant, client = legacy_assistant
    state = await _start(legacy_assistant, handover_env)
    original = getattr(handover, f"_{failing_step}_step")

    def fail_once(*args, **kwargs):
        raise handover.HandoverError("failure")

    monkeypatch.setattr(handover, f"_{failing_step}_step", fail_once)
    with pytest.raises(oauth.GoogleSignInError):
        await oauth.complete_sign_in("code", state)
    assert _legacy_state(assistant, client) == ("active", "active", True)
    assert key_store.resolve_google() is None

    monkeypatch.setattr(handover, f"_{failing_step}_step", original)
    state = _pending_state(await handover.start(conductor), handover_env[0])
    await oauth.complete_sign_in("code", state)
    assert _legacy_state(assistant, client) == ("archived", "cancelled", False)
    assert key_store.resolve_google() is not None


@pytest.mark.asyncio
async def test_key_store_failure_leaves_no_sign_in_and_a_retry_is_idempotent(
    legacy_assistant, handover_env, monkeypatch,
):
    conductor, assistant, client = legacy_assistant
    state = await _start(legacy_assistant, handover_env)
    original_store = oauth._store

    def fail_store(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(oauth, "_store", fail_store)
    with pytest.raises(oauth.GoogleSignInError, match="No CORSIA sign-in was saved"):
        await oauth.complete_sign_in("code", state)
    assert _legacy_state(assistant, client) == ("archived", "cancelled", False)
    assert key_store.resolve_google() is None

    monkeypatch.setattr(oauth, "_store", original_store)
    state = _pending_state(await handover.start(conductor), handover_env[0])
    await oauth.complete_sign_in("code", state)
    assert key_store.resolve_google() is not None


@pytest.mark.asyncio
async def test_no_client_and_multiple_assistants_refuse_without_moving(legacy_assistant, handover_env):
    conductor, assistant, client = legacy_assistant
    client.unlink()
    with pytest.raises(handover.HandoverError, match="no Desktop OAuth client file"):
        await handover.start(conductor)
    assert _legacy_state(assistant, client) == ("active", "active", False)

    db = get_db()
    other = make_session(db, prefix="assistant", title="Assistant")
    db.execute(
        "UPDATE threads SET agent_enabled=1,agent_responsibility=?,agent_domains=?,agent_notes=? WHERE id=?",
        (handover.LEGACY_ASSISTANT_RESPONSIBILITY, json.dumps(["email", "calendar", "drive", "docs"]), handover.LEGACY_ASSISTANT_NOTES, other),
    )
    db.commit()
    with pytest.raises(handover.HandoverError, match="More than one eligible old Assistant"):
        await handover.start(conductor)


@pytest.mark.asyncio
async def test_handover_tool_starts_only_after_human_gate(legacy_assistant, handover_env, monkeypatch):
    from tools import action_gate

    conductor, _, _ = legacy_assistant

    async def human_turn():
        return None

    monkeypatch.setattr(action_gate, "check_human_turn", human_turn)
    result = json.loads(await GoogleAdoptAssistantSignInTool().execute(_thread_id=conductor))
    state = _pending_state(result["url"], handover_env[0])
    assert result["next"].startswith("Give the person")
    await oauth.complete_sign_in("code", state)
    assert oauth.account_status()["status"] == "connected"


@pytest.mark.asyncio
async def test_handover_tool_never_exposes_client_or_token(legacy_assistant, handover_env, caplog, capsys):
    conductor, _, _ = legacy_assistant
    caplog.set_level(logging.DEBUG)
    tool = GoogleAdoptAssistantSignInTool()
    result = await tool.execute(_thread_id=conductor)
    captured = capsys.readouterr()
    for text in (result, caplog.text, captured.out, captured.err):
        assert SECRET not in text
        assert REFRESH not in text
    assert result == (
        "Error: Google handover can start only in a live turn written by the person. Ask them to "
        "request moving their old Assistant Google sign-in to CORSIA."
    )
    assert tool.parameters == {"type": "object", "additionalProperties": False, "properties": {}}
