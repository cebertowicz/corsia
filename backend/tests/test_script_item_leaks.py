"""A saved value never leaves an authenticated script's run toward an agent: not in its output, error text, last
error, readings, events or anything else the database keeps, not in a tool call's arguments, and not in any log.
The values are real saved values; only the sandboxed process and the tool are faked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace

import pytest
from loguru import logger

import infra.saved_logins as saved_logins
from auth_fixtures import OWNER_USER as PERSON
from automation import script_blanking, script_runner, script_tree
from automation import script_schedules as store
from automation.script_sandbox import STDOUT_CAP_BYTES, ScriptRun, ScriptSandboxError
from automation.script_tool_channel import UNCHECKED, _serve
from infra import key_store
from infra.database import get_db
from test_script_github_proxy import Writer
from test_tool_turn_identity_propagation import _CaptureTool
from tools.registry import ToolRegistry

OWNER = "leak-owner"
NAME = "billing-key"
VALUE = "Fake-billing-value-0123456789"  # gitleaks:allow - a test fixture, not a credential
PASSWORD = "Fake-login-password-0123456789"  # gitleaks:allow - a test fixture, not a credential


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch, caplog):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,model,project_id,title,created_at,updated_at) "
               "VALUES (?,?,?,?,?,?,?)", (OWNER, PERSON, "test/model", "p", "Bookkeeper", "t", "t"))
    db.commit()
    db.close()
    key_store.add_entry(NAME, "Billing key", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value(NAME, VALUE)
    saved_logins.save_login(site="https://portal.example.org", username="me", password=PASSWORD, saved_by=PERSON)
    caplog.set_level(logging.DEBUG)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG", format="{message}")
    folder = tmp_path / "script"
    folder.mkdir()
    path = folder / "leak.py"
    path.write_text("print('x')\n")
    db = get_db()
    try:
        schedule = store.create_script(OWNER, "__workspace__", script_path=path, script_sha256=store.file_sha256(path),
                                       cadence_seconds=600, series=[store.Series("cost", "usd", 0.0, 10.0)],
                                       timeout_seconds=30, now="2026-01-01T00:00:00+00:00", db=db)
        db.commit()
    finally:
        db.close()
    auth = {"state": "approved", "tree_sha256": script_tree.tree_sha256(script_tree.read_tree(path))}
    yield {"schedule": replace(schedule, payload={**(schedule.payload or {}), "authentication": auth}),
           "db": isolated_db, "caplog": caplog, "loguru": lines}
    logger.remove(sink)


def _nowhere(world) -> None:
    """Neither value in any row of the database, nor in any log line."""
    dump = "\n".join(sqlite3.connect(world["db"]).iterdump())
    for value in (VALUE, PASSWORD):
        assert value not in dump
        assert value not in world["caplog"].text and value not in "".join(world["loguru"])


def _process(monkeypatch, run=None, error: Exception | None = None):
    async def fake(*_args, **_kwargs):
        if error is not None:
            raise error
        return run

    monkeypatch.setattr(script_runner, "run_script", fake)


@pytest.mark.asyncio
async def test_a_runs_output_and_error_text_are_hidden_before_anything_keeps_them(world, monkeypatch):
    out = (f'{{"series":"cost","value":1,"note":"{VALUE}"}}\n{PASSWORD}\n'
           f'{{"event":{{"type":"qa.route","facts":{{"code":"{VALUE}"}}}}}}\n')
    _process(monkeypatch, ScriptRun(1, out.encode(), f"failed with {VALUE} and {PASSWORD}".encode(), False))
    report = await script_runner._execute(world["schedule"])
    assert VALUE not in repr(report) and PASSWORD not in repr(report)
    await asyncio.to_thread(script_runner.settle, world["schedule"], report, "2026-01-01T00:10:00+00:00", None)
    _nowhere(world)


@pytest.mark.asyncio
async def test_a_start_failure_and_an_unexpected_failure_carry_no_value(world, monkeypatch):
    _process(monkeypatch, error=ScriptSandboxError(f"could not bind {VALUE}"))
    report = await script_runner._execute(world["schedule"])
    assert report.status == "error" and VALUE not in (report.error or "")
    _process(monkeypatch, error=RuntimeError(f"boom {VALUE}"))
    monkeypatch.setattr(script_runner, "_drive_owner", lambda _thread: asyncio.sleep(0))
    await script_runner.run_claimed(world["schedule"], "2026-01-01T00:10:00+00:00")
    _nowhere(world)
    assert "RuntimeError" in "".join(world["loguru"])


@pytest.mark.asyncio
async def test_a_start_failure_that_cannot_be_checked_says_so_without_the_text(world, monkeypatch):
    def unreadable():
        raise script_blanking.Unchecked

    monkeypatch.setattr(script_blanking, "saved_values", unreadable)
    _process(monkeypatch, error=ScriptSandboxError(f"could not bind {VALUE}"))
    report = await script_runner._execute(world["schedule"])
    assert report.error and "couldn't check why for saved values" in report.error and VALUE not in report.error
    _process(monkeypatch, ScriptRun(0, VALUE.encode(), b"", False))
    report = await script_runner._execute(world["schedule"])
    assert "its output was withheld" in (report.error or "") and VALUE not in repr(report)
    _process(monkeypatch, error=RuntimeError(f"boom {VALUE}"))
    monkeypatch.setattr(script_runner, "_drive_owner", lambda _thread: asyncio.sleep(0))
    await script_runner.run_claimed(world["schedule"], "2026-01-01T00:10:00+00:00")
    _nowhere(world)


def test_over_cap_is_read_before_hiding_shrinks_the_output():
    run = ScriptRun(0, b"x" * 10, b"", False, over_cap_raw=True)
    assert run.over_cap and not ScriptRun(0, b"x" * 10, b"", False).over_cap
    assert ScriptRun(0, b"x" * (STDOUT_CAP_BYTES + 1), b"", False).over_cap


async def _call(arguments) -> tuple[dict, _CaptureTool]:
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"name": "capture_turn", "arguments": arguments}).encode() + b"\n")
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, thread_id=OWNER, registry=registry)
    return json.loads(bytes(writer.data[8:])), tool


@pytest.mark.asyncio
@pytest.mark.usefixtures("signed_in_defaults")
async def test_a_tool_call_never_hands_its_agent_a_saved_value(world):
    result, tool = await _call({"value": f"key {VALUE} and {PASSWORD}"})
    assert result["code"] == 0
    [arguments] = tool.calls
    assert VALUE not in json.dumps(arguments) and PASSWORD not in json.dumps(arguments)
    assert arguments["value"].startswith("key ")
    _nowhere(world)


def test_every_string_in_a_call_is_hidden_keys_included():
    hidden = script_blanking.structure({"a": [VALUE, {VALUE: 1}], "n": 2}, frozenset({VALUE}))
    assert VALUE not in json.dumps(hidden) and hidden["n"] == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("signed_in_defaults")
async def test_a_tool_call_that_cannot_be_checked_does_not_run(world, monkeypatch):
    def unreadable():
        raise script_blanking.Unchecked

    monkeypatch.setattr(script_blanking, "saved_values", unreadable)
    result, tool = await _call({"value": VALUE})
    assert result == {"code": 1, "stdout": "", "stderr": UNCHECKED} and tool.calls == []
