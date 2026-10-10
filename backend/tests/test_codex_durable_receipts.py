"""The managed boundary consumes only exact appended provider receipts."""

import json
import asyncio

import pytest
from codex_runtime_fixtures import declare_codex_tool_step


def lines(*records):
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def group(turn="turn", calls=("a", "b"), response="response", total=1000):
    return [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        *({"type": "response_item", "payload": {"type": "function_call", "call_id": call}}
          for call in calls),
        {"type": "token_usage_record", "payload": {
            "thread_id": "provider", "turn_id": turn, "response_id": response,
            "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                      "output_tokens": 10, "reasoning_output_tokens": 5},
            "thread_token_usage": {"input_tokens": total, "cached_input_tokens": 20,
                                   "output_tokens": 100, "reasoning_output_tokens": 5},
        }},
    ]


def cursor_for(path):
    from subagents.codex_durable_receipts import RolloutReceiptCursor

    return RolloutReceiptCursor(path, "provider")


def test_cursor_preserves_split_lines_and_all_serial_group_members(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(lines(*group(total=990)))
    cursor = cursor_for(path)
    current = lines(*group())
    with path.open("ab") as stream:
        stream.write(current[:-7])
    assert cursor.read_receipt("turn", "a") is None
    with path.open("ab") as stream:
        stream.write(current[-7:])
    first = cursor.read_receipt("turn", "a")
    assert first["last"]["inputTokens"] == 100
    assert first["total"]["inputTokens"] == 1000
    assert cursor.read_receipt("turn", "b") == first
    assert cursor.read_receipt("turn", "unknown") is None


def test_cursor_keeps_following_group_in_same_chunk(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.touch()
    cursor = cursor_for(path)
    path.write_bytes(lines(*group(), *group(calls=("c",), response="second", total=1100)))
    assert cursor.read_receipt("turn", "a")["total"]["inputTokens"] == 1000
    assert cursor.read_receipt("turn", "c")["total"]["inputTokens"] == 1100


def test_small_read_chunks_preserve_complete_receipt(tmp_path, monkeypatch):
    from subagents.codex_durable_receipts import RolloutReceiptCursor
    monkeypatch.setattr(RolloutReceiptCursor, "CHUNK_BYTES", 13)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    cursor = cursor_for(path)
    data = lines(*group())
    path.write_bytes(data)
    receipt = None
    for _ in range(len(data)):
        receipt = cursor.read_receipt("turn", "b")
        if receipt is not None:
            break
    assert receipt["last"]["inputTokens"] == 100


@pytest.mark.parametrize("bad_count", [None, -1, True, "100"])
def test_malformed_usage_does_not_authorize_tool_release(tmp_path, bad_count):
    from subagents.codex_durable_receipts import ReceiptArtifactError
    path = tmp_path / "rollout.jsonl"
    path.touch()
    cursor = cursor_for(path)
    records = group()
    records[-1]["payload"]["usage"]["input_tokens"] = bad_count
    path.write_bytes(lines(*records))
    with pytest.raises(ReceiptArtifactError, match="input_tokens"):
        cursor.read_receipt("turn", "a")


@pytest.mark.parametrize("damage", ["truncate", "replace", "missing", "malformed"])
def test_cursor_fails_explicitly_on_artifact_damage(tmp_path, damage):
    from subagents.codex_durable_receipts import ReceiptArtifactError

    path = tmp_path / "rollout.jsonl"
    path.write_bytes(lines(*group(turn="old")))
    cursor = cursor_for(path)
    if damage == "truncate":
        path.write_bytes(b"")
    elif damage == "replace":
        path.rename(tmp_path / "old.jsonl")
        path.write_bytes(lines(*group()))
    elif damage == "missing":
        path.unlink()
    else:
        with path.open("ab") as stream:
            stream.write(b"bad json\n")
    with pytest.raises(ReceiptArtifactError):
        cursor.read_receipt("turn", "a")


def test_pre_turn_partial_record_cannot_become_current_receipt(tmp_path):
    from subagents.codex_durable_receipts import ReceiptArtifactError

    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b'{"type":')
    with pytest.raises(ReceiptArtifactError, match="complete"):
        cursor_for(path)


def test_managed_launch_governs_direct_tools():
    from subagents.managed_launch import (
        codex_governance_config_args, codex_app_server_config_override_args,
    )

    assert "features.code_mode=false" in codex_governance_config_args()
    with pytest.raises(ValueError, match="governed"):
        codex_app_server_config_override_args(("features.code_mode=true",))


@pytest.mark.asyncio
async def test_current_receipt_gates_serial_callbacks_and_deduplicates_late_stdout(tmp_path, monkeypatch):
    from test_codex_tool_control_lifecycle import Process, managed_session, tool_call
    from test_checkpoint_codex_capacity import target

    proc, executed, events = Process(), [], []
    async def emit(event):
        events.append(event)
    session = managed_session(proc, on_event=emit)
    declare_codex_tool_step(session, "a", persist=False)
    declare_codex_tool_step(session, "b", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    session.tool_context.target = target()
    async def execute(*_args, **kwargs):
        executed.append(kwargs["provider_tool_call_id"])
        session.tool_context.accept_tool_result({"contentItems": [{"type": "inputText", "text": " x" * 30}]})
        return "saved"
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    first = tool_call()
    first["params"]["callId"] = "a"
    await session._handle_message(first)
    await asyncio.sleep(0)
    assert executed == [] and proc.writes == []
    path.write_bytes(lines(*group()))
    await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
    tokens = session.tool_context.tokens
    second = tool_call(rpc=901)
    second["params"]["callId"] = "b"
    await session._handle_message(second)
    await session._handle_message(first)
    await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
    assert executed == ["a", "b"]
    assert session.tool_context.tokens > tokens
    tokens = session.tool_context.tokens
    usage = dict(session._usage_by_turn["turn"])
    usage["total"] = {**usage["total"], "totalTokens": 1100}
    await session._handle_message({"method": "thread/tokenUsage/updated", "params": {
        "threadId": "provider", "turnId": "turn", "tokenUsage": usage,
    }})
    assert session.tool_context.tokens == tokens
    calls = [event for event in events if event["kind"] == "call_usage"]
    assert len(calls) == 1
    assert calls[0]["usage"]["prompt_tokens"] == 80
    assert len(proc.writes) == 2


@pytest.mark.asyncio
async def test_managed_invalid_dynamic_callback_uses_existing_rpc_error():
    from test_codex_tool_control_lifecycle import Process, managed_session, tool_call

    proc = Process()
    session = managed_session(proc)
    frame = tool_call()
    frame["params"]["arguments"] = "not-an-object"

    await session._handle_message(frame)
    await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)

    assert proc.returncode is None
    assert proc.writes == [{
        "id": 900,
        "error": {
            "code": -32602,
            "message": "Dynamic tool arguments must be an object",
        },
    }]


@pytest.mark.asyncio
async def test_code_mode_receipt_can_use_response_item_id(tmp_path, monkeypatch):
    from test_codex_tool_control_lifecycle import Process, managed_session, tool_call
    from test_checkpoint_codex_capacity import target

    proc, executed = Process(), []
    session = managed_session(proc)
    declare_codex_tool_step(session, "exec-normalized-call", persist=False)
    session.tool_context.target = target()
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    path.write_bytes(lines(
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call",
            "id": "exec-normalized-call",
            "name": "exec",
            "call_id": "raw-code-mode-call",
            "input": "text(await tools.read_file({}));\n",
        }},
        group(calls=())[1],
    ))

    async def execute(*_args, **kwargs):
        executed.append(kwargs["provider_tool_call_id"])
        session.tool_context.accept_tool_result({
            "contentItems": [{"type": "inputText", "text": "saved"}],
        })
        return "saved"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    frame = tool_call()
    frame["params"]["tool"] = "read_file"
    frame["params"]["callId"] = "exec-normalized-call"

    await session._handle_message(frame)
    await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)

    assert executed == ["exec-normalized-call"]
    assert len(proc.writes) == 1


def test_response_item_id_and_call_id_both_own_receipt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.touch()
    cursor = cursor_for(path)
    path.write_bytes(lines(
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call",
            "id": "normalized-item-id",
            "name": "exec",
            "call_id": "raw-code-mode-call",
            "input": "text(await tools.read_file({}));\n",
        }},
        group(calls=())[1],
    ))

    receipt = cursor.read_receipt("turn", "normalized-item-id")
    assert receipt["last"]["inputTokens"] == 100
    assert cursor.read_receipt("turn", "raw-code-mode-call") == receipt
    assert cursor.read_receipt("turn", "read_file") is None


def test_response_item_id_waits_for_its_current_receipt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.touch()
    cursor = cursor_for(path)

    def exec_call(item_id, call_id):
        return {"type": "response_item", "payload": {
            "type": "custom_tool_call",
            "id": item_id,
            "name": "exec",
            "call_id": call_id,
            "input": 'await tools.read_file({});',
        }}

    records = group(calls=())
    path.write_bytes(lines(records[0], exec_call("stale-item", "raw-stale"), records[1]))
    current_exec = lines(exec_call("canonical-current", "raw-current"))
    with path.open("ab") as stream:
        stream.write(current_exec[:-1])

    assert cursor.read_receipt("turn", "canonical-current") is None

    with path.open("ab") as stream:
        stream.write(current_exec[-1:])
    assert cursor.read_receipt("turn", "canonical-current") is None

    with path.open("ab") as stream:
        stream.write(lines(group(calls=(), response="current", total=1100)[1]))
    current = cursor.read_receipt("turn", "canonical-current")
    assert current["total"]["inputTokens"] == 1100
    assert cursor.read_receipt("turn", "stale-item")["total"]["inputTokens"] == 1000


@pytest.mark.asyncio
async def test_resumed_stdout_cannot_bill_a_prior_turn():
    from test_codex_tool_control_lifecycle import Process, managed_session
    events = []
    async def emit(event):
        events.append(event)
    session = managed_session(Process(), on_event=emit)
    await session._handle_message({"method": "thread/tokenUsage/updated", "params": {
        "threadId": "provider", "turnId": "old-turn", "tokenUsage": {
            "last": {"inputTokens": 100, "outputTokens": 10},
            "total": {"inputTokens": 1000, "outputTokens": 100},
        },
    }})
    assert events == []
    assert session._usage_by_turn == {}


@pytest.mark.asyncio
async def test_parallel_callbacks_share_receipt_and_accumulate_both_results(tmp_path, monkeypatch):
    from test_codex_tool_control_lifecycle import Process, managed_session, tool_call
    from test_checkpoint_codex_capacity import target
    session = managed_session(Process())
    declare_codex_tool_step(session, "a", persist=False)
    declare_codex_tool_step(session, "b", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    session.tool_context.target = target()
    path.write_bytes(lines(*group()))
    counts = []
    async def execute(*_args, **_kwargs):
        await asyncio.sleep(0)
        session.tool_context.accept_tool_result({"contentItems": [{"type": "inputText", "text": " x" * 30}]})
        counts.append(session.tool_context.tokens)
        return "saved"
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    for rpc, call in ((900, "a"), (901, "b")):
        frame = tool_call(rpc=rpc)
        frame["params"]["callId"] = call
        await session._handle_message(frame)
    await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
    assert len(counts) == 2 and counts[1] > counts[0] > 110
    assert session._turn_volume.spent_readings("turn") == 1
    assert len(session._proc.writes) == 2


@pytest.mark.asyncio
async def test_late_older_stdout_cannot_rewind_occupancy_or_charge_again():
    from test_codex_tool_control_lifecycle import Process, managed_session
    from subagents.codex_app_server_notifications import record_usage
    session = managed_session(Process())
    first = {"last": {"inputTokens": 100, "outputTokens": 10},
             "total": {"inputTokens": 1000, "outputTokens": 100}}
    second = {"last": {"inputTokens": 120, "outputTokens": 10},
              "total": {"inputTokens": 1120, "outputTokens": 110}}
    await record_usage(session, "turn", first)
    await record_usage(session, "turn", second)
    await record_usage(session, "turn", first)
    await record_usage(session, "turn", second)
    assert session.tool_context.tokens == 130
    assert session._turn_volume.spent_readings("turn") == 2
    assert session._turn_volume.take("turn").total == 240


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["eof", "error", "complete"])
async def test_pending_receipt_is_cancelled_by_its_existing_terminal_owner(tmp_path, terminal):
    from test_codex_tool_control_lifecycle import Process, managed_session, tool_call
    proc = Process()
    session = managed_session(proc)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    session._reader = asyncio.create_task(session._read_loop())
    proc.send(tool_call())
    await asyncio.sleep(0.01)
    assert session._tool_requests.tasks and not proc.writes
    if terminal == "eof":
        proc.kill()
        await asyncio.wait_for(session._reader, 1)
    elif terminal == "error":
        await session._handle_message({"method": "error", "params": {
            "threadId": "provider", "turnId": "turn", "error": {"message": "transport failed"},
        }})
    else:
        await session._handle_turn_completed({"id": "turn", "status": "interrupted"})
    assert not session._tool_requests.tasks and not proc.writes
    await session.terminate(timeout=0.1)


def test_fresh_lazy_artifact_binds_its_session_before_usage(tmp_path):
    from subagents.codex_durable_receipts import RolloutReceiptCursor, ReceiptArtifactError
    path = tmp_path / "rollout.jsonl"
    cursor = RolloutReceiptCursor(path, "provider", fresh=True)
    path.write_bytes(lines({"type": "session_meta", "payload": {"id": "wrong"}}, *group()))
    with pytest.raises(ReceiptArtifactError, match="identity"):
        cursor.read_receipt("turn", "a")


@pytest.mark.parametrize("field", ["thread_id", "turn_id", "response_id"])
def test_wrong_durable_identity_never_satisfies_a_call(tmp_path, field):
    from subagents.codex_durable_receipts import ReceiptArtifactError
    path = tmp_path / "rollout.jsonl"
    path.touch()
    cursor = cursor_for(path)
    records = group()
    records[-1]["payload"][field] = None if field == "response_id" else "wrong"
    path.write_bytes(lines(*records))
    with pytest.raises(ReceiptArtifactError, match="identity"):
        cursor.read_receipt("turn", "a")


@pytest.mark.parametrize("late_stdout", [False, True])
def test_frozen_prefix_uses_durable_receipt_and_keeps_later_tool_growth(tmp_path, late_stdout):
    from core.checkpoint_codex_context import frozen_context_tokens
    from test_checkpoint_codex_capacity import target

    records = group()
    records.append({"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "a", "output": " x" * 40,
    }})
    if late_stdout:
        records.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": records[-2]["payload"]["usage"],
            "total_token_usage": records[-2]["payload"]["thread_token_usage"],
        }}})
    path = tmp_path / "rollout.jsonl"
    data = lines(*records)
    path.write_bytes(data + lines(*group(total=1100)))
    tokens = frozen_context_tokens(path, len(data), target=target(), fallback_tokens=9000)
    assert 150 < tokens < 190
