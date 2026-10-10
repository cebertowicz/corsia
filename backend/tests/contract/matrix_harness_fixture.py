"""Inference-free Claude/Codex protocol processes for the lifecycle matrix."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def install_fixture_binaries(root: Path) -> dict[str, str]:
    """Create executable entry points inside the disposable matrix root."""
    backend_root = Path(__file__).resolve().parents[2]
    binary_dir = root / "fixture-bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    codex_home = root / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_payload = json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": "matrix-fixture-only"},
    })
    model_payload = json.dumps({
        "models": [{"slug": "fixture-codex", "multi_agent_version": None}],
    })
    (codex_home / "auth.json").write_text(auth_payload)
    (codex_home / "models_cache.json").write_text(model_payload)
    runtime_home = root / "state" / "runtime" / "codex-app-server-home"
    runtime_home.mkdir(parents=True, exist_ok=True)
    (runtime_home / "auth.json").write_text(auth_payload)
    (runtime_home / "models_cache.json").write_text(model_payload)
    result: dict[str, str] = {}
    for harness, name in (("claude-code", "claude"), ("codex", "codex")):
        path = binary_dir / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(backend_root)!r})\n"
            "from tests.contract.matrix_harness_fixture import main\n"
            f"main({harness!r})\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        result[harness] = str(path)
    return result


def _fixture_url(path: str) -> str:
    port = int(os.environ["BACKEND_PORT"])
    return f"http://localhost:{port}/api/test/{path.lstrip('/')}"


def _get_json(path: str, *, timeout: float = 180.0) -> dict[str, Any]:
    with urllib.request.urlopen(_fixture_url(path), timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError("matrix fixture returned a non-object")
    return payload


def _next_reply(harness: str) -> dict[str, Any]:
    return dict(_get_json(f"harness-responses/{urllib.parse.quote(harness)}/next")["reply"])


def _claude_bridge() -> tuple[str, str] | None:
    """Read the invocation-local MCP loopback endpoint and token."""
    try:
        raw = sys.argv[sys.argv.index("--mcp-config") + 1]
    except (ValueError, IndexError):
        return None
    try:
        config = json.loads(Path(raw).read_text())
        server = config["mcpServers"]["corsia"]
        env = server["env"]
        return str(env["CORSIA_CC_TOOL_URL"]), str(env["CORSIA_CC_TOOL_TOKEN"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Claude fixture received an invalid CORSIA MCP config") from exc


def _argument(name: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _claude_model() -> str:
    return _argument("--model") or "fixture-cc"


def _call_claude_bridge(call: dict[str, Any], call_id: str) -> str:
    bridge = _claude_bridge()
    if bridge is None:
        raise RuntimeError("Claude fixture has no CORSIA tool bridge")
    endpoint, token = bridge
    raw_arguments = call.get("arguments_json", call.get("arguments", "{}"))
    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    body = json.dumps({
        "invocation_id": f"matrix-{uuid.uuid4().hex}",
        "provider_tool_call_id": call_id,
        "name": str(call.get("name") or ""),
        "arguments": arguments or {},
    }).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-CORSIA-Tool-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read().decode("utf-8", "replace")


def _await_reply(reply: dict[str, Any], cancelled: threading.Event) -> bool:
    delay = float(reply.get("delay_ms") or 0) / 1000.0
    if delay and cancelled.wait(delay):
        return False
    gate = reply.get("completion_gate")
    if isinstance(gate, str) and gate:
        _get_json(f"mock-gates/{urllib.parse.quote(gate)}/wait")
    return not cancelled.is_set()


class _Emitter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, payload: dict[str, Any]) -> None:
        with self._lock:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def _usage(reply: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": int(reply.get("prompt_tokens") or 100),
        "output_tokens": int(reply.get("completion_tokens") or 50),
        "cache_read_input_tokens": int(reply.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": 0,
    }


def _claude_worker(emit: _Emitter, cancelled: threading.Event) -> None:
    reply = _next_reply("claude-code")
    if not _await_reply(reply, cancelled):
        return
    blocks: list[dict[str, Any]] = []
    if reply.get("content"):
        blocks.append({"type": "text", "text": str(reply["content"])})
    calls = list(reply.get("tool_calls") or ())
    for call in calls:
        raw_arguments = call.get("arguments_json", call.get("arguments", "{}"))
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        blocks.append({
            "type": "tool_use", "id": str(call.get("id") or uuid.uuid4().hex),
            "name": f"mcp__corsia__{str(call.get('name') or '')}",
            "input": arguments or {},
        })
    finish = str(reply.get("finish_reason") or "stop")
    emit({
        "type": "assistant",
        "message": {
            "id": f"msg_{uuid.uuid4().hex}", "role": "assistant",
            "model": _claude_model(), "content": blocks,
            "stop_reason": "tool_use" if reply.get("tool_calls") else finish,
            "usage": _usage(reply),
        },
    })
    if calls:
        for call, block in zip(calls, blocks[-len(calls):], strict=True):
            if cancelled.wait(0.05):
                return
            result = _call_claude_bridge(call, str(block["id"]))
            emit({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result", "tool_use_id": block["id"],
                        "content": result, "is_error": False,
                    }],
                },
            })
        _claude_worker(emit, cancelled)
    else:
        emit({
            "type": "result", "subtype": "success", "is_error": False,
            "result": str(reply.get("content") or ""), "total_cost_usd": 0.0,
            "num_turns": 1, "stop_reason": finish, "usage": _usage(reply),
        })


def _run_claude() -> None:
    emit = _Emitter()
    bridge = _claude_bridge()
    process_model = _claude_model()
    emit({
        "type": "system", "subtype": "init", "session_id": uuid.uuid4().hex,
        "model": process_model,
        "tools": (["mcp__corsia__read_file"] if bridge else []),
        "mcp_servers": ([{"name": "corsia", "status": "connected"}] if bridge else []),
        "cwd": os.getcwd(),
    })
    active = threading.Event()
    selected_model = process_model
    for raw in sys.stdin:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if frame.get("type") == "control_request":
            request = frame.get("request") or {}
            subtype = request.get("subtype")
            response_body: dict[str, Any] = {}
            if subtype == "list_models":
                response_body = {"models": [
                    {
                        "value": "fixture-cc", "displayName": "Fixture CC",
                        "resolvedModel": "fixture-cc", "supportsEffort": True,
                        "supportedEffortLevels": ["low", "high"],
                    },
                    {
                        "value": "fixture-cc-pressure",
                        "displayName": "Fixture CC Pressure",
                        "resolvedModel": "fixture-cc-pressure",
                        "supportsEffort": True,
                        "supportedEffortLevels": ["low", "high"],
                    },
                ]}
            elif subtype == "set_model":
                selected_model = str(request.get("model") or selected_model)
            elif subtype == "get_context_usage":
                window = 131_072 if selected_model.endswith("-pressure") else 393_216
                response_body = {
                    "model": selected_model,
                    "totalTokens": 30_000,
                    "rawMaxTokens": window,
                    "maxTokens": window,
                    "categories": [
                        {"name": "MCP tools", "kind": "used", "tokens": 24_000},
                    ],
                }
            emit({
                "type": "control_response",
                "response": {
                    "request_id": frame.get("request_id"), "subtype": "success",
                    "response": response_body,
                },
            })
            if subtype == "interrupt":
                active.set()
                emit({
                    "type": "result", "subtype": "success", "is_error": False,
                    "result": "interrupted", "total_cost_usd": 0.0,
                    "num_turns": 1, "stop_reason": "interrupt",
                })
            continue
        if frame.get("type") != "user":
            continue
        active = threading.Event()
        threading.Thread(
            target=_claude_worker, args=(emit, active), daemon=False,
        ).start()


class _CodexFixture:
    def __init__(self) -> None:
        self.emit = _Emitter()
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.rollout: Path | None = None
        self.cancelled = threading.Event()
        self.pending_tools: dict[int, tuple[str, str]] = {}
        self.next_server_id = 10_000
        self.total_input = 0
        self.total_output = 0

    def _append_rollout(self, record: dict[str, Any]) -> None:
        assert self.rollout is not None
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        with self.rollout.open("a") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _ensure_thread(self, requested: str | None = None) -> None:
        self.thread_id = requested or self.thread_id or f"thread_{uuid.uuid4().hex}"
        if self.rollout is None:
            home = Path(os.environ.get("CODEX_HOME") or os.environ["HOME"])
            self.rollout = home / "sessions" / f"{self.thread_id}.jsonl"
        if not self.rollout.exists():
            self._append_rollout({"type": "session_meta", "payload": {"id": self.thread_id}})

    def _token_usage(self, reply: dict[str, Any]) -> dict[str, Any]:
        last_input = int(reply.get("prompt_tokens") or 100)
        last_output = int(reply.get("completion_tokens") or 50)
        self.total_input += last_input
        self.total_output += last_output
        return {
            "last": {
                "inputTokens": last_input, "outputTokens": last_output,
                "cachedInputTokens": int(reply.get("cache_read_input_tokens") or 0),
                "reasoningOutputTokens": 0,
            },
            "total": {
                "inputTokens": self.total_input, "outputTokens": self.total_output,
                "cachedInputTokens": 0, "reasoningOutputTokens": 0,
            },
            "modelContextWindow": 393_216,
        }

    def _record_tool_receipt(self, call_id: str, usage: dict[str, Any]) -> None:
        assert self.thread_id is not None and self.turn_id is not None
        self._append_rollout({
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": self.turn_id},
        })
        self._append_rollout({
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": call_id},
        })
        last, total = usage["last"], usage["total"]
        self._append_rollout({
            "type": "token_usage_record",
            "payload": {
                "thread_id": self.thread_id, "turn_id": self.turn_id,
                "response_id": f"response_{uuid.uuid4().hex}",
                "usage": {
                    "input_tokens": last["inputTokens"],
                    "output_tokens": last["outputTokens"],
                    "cached_input_tokens": last["cachedInputTokens"],
                    "reasoning_output_tokens": 0,
                },
                "thread_token_usage": {
                    "input_tokens": total["inputTokens"],
                    "output_tokens": total["outputTokens"],
                    "cached_input_tokens": total["cachedInputTokens"],
                    "reasoning_output_tokens": 0,
                },
            },
        })

    def _worker(self) -> None:
        assert self.thread_id is not None and self.turn_id is not None
        reply = _next_reply("codex")
        if not _await_reply(reply, self.cancelled):
            return
        usage = self._token_usage(reply)
        self.emit({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": self.thread_id, "turnId": self.turn_id,
                "tokenUsage": usage,
            },
        })
        calls = list(reply.get("tool_calls") or ())
        if calls:
            call = calls[0]
            call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
            raw_arguments = call.get("arguments_json", call.get("arguments", "{}"))
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            # Fault injection changes only the provider's receipt, never the RPC.
            self._record_tool_receipt(call.get("receipt_call_id", call_id), usage)
            self.emit({
                "method": "item/started",
                "params": {
                    "threadId": self.thread_id, "turnId": self.turn_id,
                    "item": {
                        "id": call_id, "type": "dynamicToolCall",
                        "tool": str(call.get("name") or ""), "arguments": arguments or {},
                    },
                },
            })
            request_id = self.next_server_id
            self.next_server_id += 1
            self.pending_tools[request_id] = (call_id, str(call.get("name") or ""))
            self.emit({
                "id": request_id, "method": "item/tool/call",
                "params": {
                    "threadId": self.thread_id, "turnId": self.turn_id,
                    "callId": call_id, "tool": str(call.get("name") or ""),
                    "arguments": arguments or {},
                },
            })
            return
        content = str(reply.get("content") or "")
        if content:
            self.emit({
                "method": "item/completed",
                "params": {
                    "threadId": self.thread_id, "turnId": self.turn_id,
                    "item": {
                        "id": f"message_{uuid.uuid4().hex}",
                        "type": "agentMessage", "text": content,
                    },
                },
            })
        self.emit({
            "method": "turn/completed",
            "params": {"turn": {"id": self.turn_id, "status": "completed"}},
        })

    def request(self, frame: dict[str, Any]) -> None:
        request_id = frame["id"]
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "initialize":
            self.emit({"id": request_id, "result": {}})
        elif method == "model/list":
            self.emit({
                "id": request_id,
                "result": {"data": [
                    {
                        "id": "fixture-codex", "model": "fixture-codex",
                        "displayName": "Fixture Codex", "hidden": False,
                        "inputModalities": ["text"],
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "high"},
                        ],
                    },
                    {
                        "id": "fixture-codex-pressure",
                        "model": "fixture-codex-pressure",
                        "displayName": "Fixture Codex Pressure", "hidden": False,
                        "inputModalities": ["text"],
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "high"},
                        ],
                    },
                ]},
            })
        elif method in {"thread/start", "thread/resume"}:
            self._ensure_thread(params.get("threadId"))
            self.emit({
                "id": request_id,
                "result": {
                    "thread": {"id": self.thread_id, "path": str(self.rollout)},
                    "model": "fixture-codex",
                },
            })
        elif method == "thread/inject_items":
            self.emit({"id": request_id, "result": {}})
        elif method in {"turn/start", "turn/steer"}:
            self.turn_id = f"turn_{uuid.uuid4().hex}"
            self.cancelled = threading.Event()
            self.emit({"id": request_id, "result": {"turn": {"id": self.turn_id}}})
            threading.Thread(target=self._worker, daemon=True).start()
        elif method == "turn/interrupt":
            self.cancelled.set()
            self.emit({"id": request_id, "result": {}})
            if self.turn_id is not None:
                self.emit({
                    "method": "turn/completed",
                    "params": {"turn": {"id": self.turn_id, "status": "interrupted"}},
                })
        else:
            self.emit({"id": request_id, "result": {}})

    def response(self, frame: dict[str, Any]) -> None:
        pending = self.pending_tools.pop(int(frame["id"]), None)
        if pending is None or self.thread_id is None or self.turn_id is None:
            return
        call_id, tool = pending
        result = frame.get("result") or {"success": False, "contentItems": []}
        self.emit({
            "method": "item/completed",
            "params": {
                "threadId": self.thread_id, "turnId": self.turn_id,
                "item": {
                    "id": call_id, "type": "dynamicToolCall", "tool": tool,
                    "success": bool(result.get("success")),
                    "contentItems": result.get("contentItems") or [],
                },
            },
        })
        threading.Thread(target=self._worker, daemon=True).start()


def _run_codex() -> None:
    fixture = _CodexFixture()
    for raw in sys.stdin:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "id" in frame and "method" in frame:
            fixture.request(frame)
        elif "id" in frame and ("result" in frame or "error" in frame):
            fixture.response(frame)


def main(harness: str) -> None:
    """Run one generated fixture executable."""
    if harness == "claude-code":
        _run_claude()
    elif harness == "codex":
        _run_codex()
    else:  # pragma: no cover - generated wrappers pass literals above
        raise SystemExit(f"unknown matrix harness {harness}")
