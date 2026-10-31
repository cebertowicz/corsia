
from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import sys
import textwrap
import uuid

import pytest

from tests.conftest import _reset_db_pool, _seed_test_model_catalog

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def cc_target(selector: str = "opus"):
    """Exact catalog target used by direct CC runtime tests."""
    from llm.resolution import ExecutionTarget

    reported = {
        "opus": "claude-opus-4-8",
        "sonnet": "claude-sonnet-4-8",
        "fable": "claude-fable-5",
    }.get(selector, selector)
    return ExecutionTarget(
        catalog_id=f"claude-code/{selector}",
        harness="claude-code",
        provider="claude-code",
        backend="cc",
        transport_model=selector,
        expected_reported_model=reported,
        context_window=967_000 if selector == "fable" else 200_000,
        available=True,
        availability_reason=None,
        display_name=selector,
        supports_reasoning=True,
    )


class StubCCInvocation:
    """Supply the launch-description contract for CC process test doubles."""

    def invocation(self):
        from subagents.cc_stream import ClaudeInvocation

        return ClaudeInvocation(
            binary="claude",
            permission_mode="bypassPermissions",
        )


async def _preadmitted_input(thread_id: str, text: str, kwargs: dict) -> dict:
    """Add the canonical test FIFO admission to direct harness arguments."""
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    turn_id = kwargs.setdefault("turn_id", str(uuid.uuid4()))
    allow_provider_resume = bool(kwargs.pop("allow_provider_resume", True))
    metadata = dict(kwargs.get("agent_meta") or {})
    if kwargs.get("edited"):
        metadata["edited"] = True
    admission = admit_input(
        thread_id,
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"test-input:{turn_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
        metadata=metadata,
    )
    if admission.batch is None:
        raise AssertionError(f"test input did not start Turn {turn_id}")
    kwargs.setdefault("preadmitted_turn", admission.batch.identity)
    kwargs.setdefault("persist_input_once", _no_persist)
    kwargs.setdefault(
        "mark_accepted", lambda: settle_test_input(admission.batch),
    )
    if "prepared_context" not in kwargs:
        kwargs["prepared_context"] = await prepared_cc_input(
            thread_id,
            text,
            kwargs["target"],
            preadmitted_turn_id=turn_id,
            allow_provider_resume=allow_provider_resume,
        )
    return kwargs


def _own_home(home) -> None:
    """The directory a test launches in is the signed-in Default's home: an account's launch uses no other."""
    from tests.vendor_account_fixtures import point_default_home

    point_default_home("claude-code", home)


async def feed_main_with_persisted_input(
    thread_id: str,
    text: str,
    **kwargs,
):
    """Drive direct-runtime tests through the current Turn admission contract."""
    import subagents.cc_runtime as rt

    from contextlib import nullcontext
    from unittest.mock import patch

    home = kwargs.get("claude_dir")
    if home:
        _own_home(home)
    scope = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}) if home else nullcontext()
    with scope:
        prepared = await _preadmitted_input(thread_id, text, kwargs)
        return await rt.feed_main(thread_id, text, **prepared)


async def feed_with_persisted_input(
    thread_id: str,
    text: str,
    **kwargs,
):
    """Drive delegated-runtime tests through the current Turn admission contract."""
    import subagents.cc_runtime as rt
    from contextlib import nullcontext
    from unittest.mock import patch

    home = kwargs.get("claude_dir")
    if home:
        _own_home(home)
    scope = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}) if home else nullcontext()
    with scope:
        prepared = await _preadmitted_input(thread_id, text, kwargs)
        return await rt.feed(thread_id, text, **prepared)


async def ensure_live_with_persisted_input(
    thread_id: str,
    text: str,
    **kwargs,
):
    """Drive transition tests through the same canonical FIFO admission."""
    import subagents.cc_runtime as rt

    from contextlib import nullcontext
    from unittest.mock import patch

    home = kwargs.get("claude_dir")
    if home:
        _own_home(home)
    scope = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}) if home else nullcontext()
    with scope:
        prepared = await _preadmitted_input(thread_id, text, kwargs)
        return await rt.ensure_live_then_feed(thread_id, text, **prepared)


async def _no_persist() -> None:
    return None


def settle_test_input(batch) -> None:
    """Project a direct-runtime test acknowledgment into the canonical FIFO."""
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.time import utc_now_iso

    attempt_id = f"test-write:{batch.turn_id}"
    begin_batch_write(
        batch,
        attempt_id=attempt_id,
        started_at=utc_now_iso(),
    )
    settle_batch_write(attempt_id, outcome="accepted")


async def prepared_cc_input(
    thread_id: str,
    text: str,
    target,
    *,
    preadmitted_turn_id: str | None = None,
    allow_provider_resume: bool = True,
):
    """Run the real CC destination preflight for a direct-runtime test input."""
    from infra.database import get_db
    from infra.send_preflight import context_preflight
    from registry.listing import find_for_thread
    from subagents import cc_runtime

    db = get_db()
    try:
        thread_row = db.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        has_session = bool(
            find_for_thread(thread_id, "claude-code", "session", db=db)
        )
    finally:
        db.close()
    if thread_row is None:
        raise AssertionError(f"missing test thread {thread_id}")
    live = cc_runtime.is_live(thread_id)
    result = await context_preflight(
        thread_id=thread_id,
        thread_row=thread_row,
        provider="cc",
        target=target,
        runtime_truth={"cc_live": live, "cc_has_session": live or has_session},
        current_user_content=text,
        current_user_source_content=text,
        preadmitted_turn_id=preadmitted_turn_id,
        allow_provider_resume=allow_provider_resume,
    )
    if result["status"] != "allowed" or result["prepared_context"] is None:
        raise AssertionError(f"CC test preflight refused input: {result}")
    return result["prepared_context"]


class _TestNoOpClose:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


@pytest.fixture
def child_db(tmp_path, monkeypatch, _cc_runtime_cleanup):
    from infra.schema import SCHEMA_SQL

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _seed_test_model_catalog(conn)
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import set_account_state
    from vendor_account_fixtures import seed_default_account

    account = seed_default_account('claude-code', db=conn)
    account = set_account_state(account['id'], 'signed_in', expected_revision=0, db=conn)
    receipt = capture_receipt('claude-code', account_id=account['id'], db=conn)
    models = {row['id']: {} for row in conn.execute("SELECT id FROM models WHERE harness='claude-code'")}
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models=models, db=conn)
    conn.commit()
    wrapper = _TestNoOpClose(conn)

    from infra import config, database

    _reset_db_pool(database)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)

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


async def _wait(pred, tries=400, delay=0.02):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


def trust_live_tool_step_ids(monkeypatch) -> None:
    """Resolve provider calls from the live map instead of durable Assistant Steps.

    For route suites that own bridge auth, transport, or settlement. Declared
    name/args binding and one-time claims are pinned by
    test_cc_tool_result_echo_binding against real persisted steps.
    """
    from api import cc_attach

    async def _declared(live, _thread_id, body):
        call_id = str(body.get("provider_tool_call_id") or "")
        return call_id, live.tool_step_ids[call_id]

    monkeypatch.setattr(cc_attach, "_claim_declared_tool_call", _declared)


def _seed_parent(db, sid="parent", user="steve"):
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
        (sid, user, "claude-code/sonnet", "t", "t"),
    )
    db.commit()


def persist_bridge_tool_result(
    db,
    live,
    *,
    thread_id: str,
    provider_tool_call_id: str,
    content: str,
    is_error: bool = False,
    step_id: str | None = None,
    name: str | None = None,
):
    """Persist the canonical tool row exactly as the loopback bridge does.

    In production every CC tool executes through the backend bridge, which
    persists the canonical result bytes *before* the provider can echo the
    tool_result event (harness_tool_bridge.execute_tool). Tests that emit
    ``tool_result`` events directly must first replicate that persistence or
    the echo is an impossible-in-production orphan and ``bind_tool_result_echo``
    correctly rejects it. Returns the delivery; use
    ``delivery.render_for_model(..., include_invocation_reference=True)`` when
    the test needs the exact marker-bearing echo CC would send back.
    """
    from core.tool_result_delivery import persist_tool_execution_result

    step_id = step_id or live.tool_step_ids[provider_tool_call_id]
    name = name or live.tool_names.get(provider_tool_call_id) or "tool"
    delivery = persist_tool_execution_result(
        thread_id=thread_id,
        turn_id=live.corsia_turn.turn_id,
        invocation_id=f"inv-{uuid.uuid4()}",
        assistant_step_id=step_id,
        provider_tool_call_id=provider_tool_call_id,
        name=name,
        arguments=None,
        content=content,
        is_error=is_error,
        db=db,
    )
    db.commit()
    return delivery


def _ready_count(casts):
    return sum(1 for sid, m in casts
               if sid == "parent" and m.get("type") == "subagent_status"
               and m.get("status") == "ready")


def _done_count(casts):
    return sum(1 for sid, m in casts
               if sid == "parent" and m.get("type") == "message_added"
               and (m.get("message", {}).get("metadata") or {}).get("type") == "subagent_done")


_FAKE_ONESHOT = textwrap.dedent(
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
    line = sys.stdin.readline()
    if line.strip():
        try:
            msg = json.loads(line)
        except Exception:
            msg = {{}}
        c = visible_content(msg.get("message", {{}}).get("content", ""))
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + c}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"echo:" + c,"total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


@pytest.fixture
def fake_claude_oneshot(tmp_path):
    p = tmp_path / "fake_claude_oneshot"
    p.write_text(_FAKE_ONESHOT.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_home(tmp_path):
    import json as _json
    proj = tmp_path / "claude-home" / "projects" / "-Users-x-proj"
    proj.mkdir(parents=True)
    lines = [
        {"type": "user", "cwd": "/Users/x/proj", "sessionId": "cc-old-1",
         "timestamp": "2026-06-09T10:00:00Z",
         "message": {"role": "user", "content": "fix the login bug"}},
        {"type": "assistant", "cwd": "/Users/x/proj", "sessionId": "cc-old-1",
         "timestamp": "2026-06-09T10:00:05Z",
         "message": {"content": [{"type": "text", "text": "On it."}]}},
    ]
    with open(proj / "cc-old-1.jsonl", "w") as f:
        for line in lines:
            f.write(_json.dumps(line) + "\n")
    return str(tmp_path / "claude-home")
