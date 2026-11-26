"""The socket that gives a script the items its approval gives it: only those, only while they pass the owner's
check, one history row each, and never through an environment variable. Also the run's wiring of both item
sockets, and that an agent's own shell never inherits their names.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from auth_fixtures import OWNER_USER as PERSON
from automation import script_clients, script_items, script_sandbox
from automation.script_item_given import SOCKET_ENV as GIVEN_ENV, given_items
from automation.script_item_proxy import SOCKET_ENV as SEND_ENV
from connections.github_agent import AgentAccountView
from infra import key_store
from infra.database import get_db
from tests.test_script_item_proxy import socket_folder, socket_parent

OWNER = "given-owner"
GIVEN = "signing-key"
SENT = "billing-key"
VALUE = "Fake-signing-value-0123456789"  # gitleaks:allow - a test fixture, not a credential
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               (OWNER, PERSON, "Bookkeeper", None, "t", "t"))
    db.commit()
    db.close()
    for name, label in ((GIVEN, "Signing key"), (SENT, "Billing key")):
        key_store.add_entry(name, label, "", added_by_thread_id=None, added_by_name=None)
        key_store.set_value(name, VALUE if name == GIVEN else "Fake-billing-value-0123456789")  # gitleaks:allow
    entries = script_items.declared(OWNER, [{"name": GIVEN, "given": True}, {"name": SENT, "place": "a.example.com"}],
                                    authenticated=True)
    folder = socket_folder()
    yield SimpleNamespace(run=script_items.for_run(OWNER, "script-1", {"saved_items": entries}, None),
                          socket=folder / "v", folder=folder, tmp=tmp_path)
    shutil.rmtree(folder, ignore_errors=True)


async def _ask(w, name) -> dict:
    async with given_items(w.socket, w.run):
        reader, writer = await asyncio.open_unix_connection(str(w.socket))
        writer.write(json.dumps({"name": name}).encode() + b"\n")
        await writer.drain()
        size = int.from_bytes(await reader.readexactly(8), "big")
        answer = json.loads(await reader.readexactly(size))
        writer.close()
    return answer


def _audit() -> list[tuple]:
    db = get_db()
    try:
        return [tuple(row) for row in db.execute(
            "SELECT login_id, item, site, thread_id, result, script_id FROM saved_login_fill_audit ORDER BY id")]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_only_a_given_item_is_answered_with_one_history_row(world):
    assert await _ask(world, GIVEN) == {"value": VALUE}
    assert _audit() == [("", GIVEN, "script", OWNER, "filled", "script-1")]
    for name in (SENT, "not-listed", None, 7):
        answer = await _ask(world, name)
        assert set(answer) == {"error"} and "doesn't give that saved item" in answer["error"]
    assert len(_audit()) == 1


@pytest.mark.asyncio
async def test_a_cleared_item_is_no_longer_given(world):
    key_store.clear_value(GIVEN)
    assert await _ask(world, GIVEN) == {"error": "Saved item is no longer available to this script."}
    assert _audit() == []


@pytest.mark.asyncio
async def test_the_socket_is_the_owners_only_and_goes_away_with_the_run(world):
    async with given_items(world.socket, world.run):
        assert world.socket.stat().st_mode & 0o777 == 0o600
    assert not world.socket.exists()


@pytest.mark.asyncio
async def test_corsia_item_prints_the_value_exactly(world):
    async def item(*args: str, socket: Path | None = world.socket) -> tuple[int, bytes, bytes]:
        env = {"PATH": os.environ.get("PATH", ""), **({GIVEN_ENV: str(socket)} if socket else {})}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(BACKEND / "cli_github.py"), "item", *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        assert process.returncode is not None
        return process.returncode, out, err

    async with given_items(world.socket, world.run):
        assert await item(GIVEN) == (0, VALUE.encode(), b"")
        code, out, err = await item(SENT)
        assert code == 1 and out == b"" and b"doesn't give that saved item" in err
        code, _, err = await item()
        assert code == 2 and b"Usage: corsia item" in err
    code, _, err = await item(GIVEN, socket=None)
    assert code == 1 and b"no saved item given to it" in err


def _fake_clients(monkeypatch, tmp_path):
    @asynccontextmanager
    async def fake_channel(*args, **kwargs):
        yield

    monkeypatch.setattr(script_clients, "tool_channel", fake_channel)
    monkeypatch.setattr(script_clients, "github_proxy", fake_channel)
    monkeypatch.setattr(script_clients, "corsia_temp_root", socket_parent)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)
    monkeypatch.setattr(script_clients, "corsia_temp_root_for_bind", lambda thread_id: tmp_path)


@pytest.mark.asyncio
async def test_a_run_gets_both_item_sockets_by_path_and_never_a_value(world, monkeypatch):
    _fake_clients(monkeypatch, world.tmp)
    view = AgentAccountView("ready", home=str(world.tmp / "agent-home"), login="example")
    async with script_clients._authenticated_client(OWNER, view, world.run) as (sockets, _, _, _, item_env):
        assert set(item_env) == {SEND_ENV, GIVEN_ENV}
        assert item_env[SEND_ENV] == str(sockets / "s") and item_env[GIVEN_ENV] == str(sockets / "v")
        assert (sockets / "s").exists() and (sockets / "v").exists()
        assert VALUE not in json.dumps(item_env)
    assert not (sockets / "s").exists() and not (sockets / "v").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["withheld", "none"])
async def test_a_withheld_run_or_one_with_no_items_gets_no_item_socket(world, monkeypatch, case):
    _fake_clients(monkeypatch, world.tmp)
    run = script_items.RunItems(OWNER, "script-1", world.run.uses, withheld=True) if case == "withheld" else None
    view = AgentAccountView("ready", home=str(world.tmp / "agent-home"), login="example")
    async with script_clients._authenticated_client(OWNER, view, run) as (sockets, _, _, _, item_env):
        assert item_env == {}
        assert not (sockets / "s").exists() and not (sockets / "v").exists()


def test_an_agents_own_shell_never_inherits_the_item_socket_names(monkeypatch):
    from tools.shell_environment import safehouse_subprocess_env

    monkeypatch.setenv(SEND_ENV, "/somewhere/s")
    monkeypatch.setenv(GIVEN_ENV, "/somewhere/v")
    env = safehouse_subprocess_env("some-thread")
    assert SEND_ENV not in env and GIVEN_ENV not in env
