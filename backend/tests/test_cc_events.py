
from __future__ import annotations

import os
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _norm(raw):
    from subagents.cc_events import normalize_event
    return normalize_event(raw)


class TestSystemEvents:
    def test_init(self):
        raw = {
            "type": "system", "subtype": "init",
            "cwd": "/tmp/work", "session_id": "abc-123",
            "tools": ["Read", "Write", "Bash"],
            "mcp_servers": [{"name": "corsia", "status": "pending"}],
            "plugins": ["superpowers@claude-plugins-official"],
            "skills": ["superpowers:using-superpowers"],
            "model": "claude-opus-4-8[1m]", "permissionMode": "bypassPermissions",
        }
        out = _norm(raw)
        assert len(out) == 1
        ev = out[0]
        assert ev["kind"] == "init"
        assert ev["session_id"] == "abc-123"
        assert ev["model"] == "claude-opus-4-8[1m]"
        assert ev["tools"] == ["Read", "Write", "Bash"]
        assert ev["cwd"] == "/tmp/work"
        assert ev["mcp_servers"] == [{"name": "corsia", "status": "pending"}]
        assert ev["plugins"] == ["superpowers@claude-plugins-official"]
        assert ev["skills"] == ["superpowers:using-superpowers"]

    def test_status(self):
        out = _norm({"type": "system", "subtype": "status", "status": "requesting", "session_id": "x"})
        assert out == [{"kind": "status", "status": "requesting"}]

    def test_hook_started_ignored(self):
        assert _norm({
            "type": "system",
            "subtype": "hook_started",
            "hook_name": "SessionStart:startup",
            "command": "cat /Users/example/.ssh/id_ed25519",
            "output": "secret",
        }) == [{
            "kind": "tool_boundary_violation",
            "capability": "hook",
        }]

    def test_non_compaction_lifecycle_hook_is_not_misclassified(self):
        out = _norm({
            "type": "system",
            "subtype": "hook_started",
            "hook_event": "SessionStart",
            "hook_id": "hook-session-start",
            "session_id": "provider-session-1",
        })
        assert out == [{
            "kind": "tool_boundary_violation",
            "capability": "hook",
        }]

    def test_hook_response_ignored(self):
        assert _norm({
            "type": "system",
            "subtype": "hook_response",
            "output": "secret",
        }) == [{
            "kind": "tool_boundary_violation",
            "capability": "hook",
        }]

    def test_init_preserves_callable_metadata_and_inert_descriptors(self):
        event = _norm({
            "type": "system",
            "subtype": "init",
            "tools": ["mcp__corsia__read_file"],
            "mcp_servers": [{"name": "corsia"}],
            "plugins": ["descriptor-only-plugin"],
            "agents": ["descriptor-only-agent"],
            "skills": ["callable-skill"],
            "slash_commands": ["callable-command"],
        })[0]

        assert event["plugins"] == ["descriptor-only-plugin"]
        assert event["agents"] == ["descriptor-only-agent"]
        assert event["skills"] == ["callable-skill"]
        assert event["slash_commands"] == ["callable-command"]

    def test_thinking_tokens_ignored(self):
        assert _norm({"type": "system", "subtype": "thinking_tokens"}) == []


class TestStreamDeltas:
    def test_text_delta(self):
        raw = {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": "PON"}},
            "session_id": "x",
        }
        assert _norm(raw) == [{"kind": "text_delta", "text": "PON"}]

    def test_thinking_delta(self):
        raw = {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0,
                      "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        }
        assert _norm(raw) == [{"kind": "thinking_delta", "text": "hmm"}]

    @pytest.mark.parametrize("usage", [
        {
            "input_tokens": 1248,
            "cache_creation_input_tokens": 6555,
            "cache_read_input_tokens": 0,
            "output_tokens": 185,
            "output_tokens_details": {"thinking_tokens": 101},
        },
        {
            "input_tokens": 2,
            "cache_creation_input_tokens": 37853,
            "cache_read_input_tokens": 0,
            "output_tokens": 9,
            "output_tokens_details": {"thinking_tokens": 0},
        },
    ])
    def test_final_message_delta_preserves_per_call_usage(self, usage):
        raw = {
            "type": "stream_event",
            "event": {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": usage,
            },
        }

        assert _norm(raw) == [{"kind": "final_call_usage", "usage": usage}]

    @pytest.mark.parametrize("event", [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": "malformed",
        },
    ])
    def test_final_message_delta_without_valid_usage_still_marks_call_boundary(
        self, event,
    ):
        raw = {"type": "stream_event", "event": event}

        assert _norm(raw) == [{"kind": "final_call_usage", "usage": None}]

    def test_signature_delta_is_typed_provenance_not_assistant_text(self):
        raw = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-123"},
            },
        }

        assert _norm(raw) == [{
            "kind": "reasoning_provenance",
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_signatures": ["sig-123"],
                "native_signature": "sig-123",
            },
        }]

    def test_non_text_stream_event_ignored(self):
        assert _norm({"type": "stream_event", "event": {"type": "message_start"}}) == []


class TestAssistant:
    def test_text_block(self):
        raw = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "PONG"}]},
            "session_id": "x",
        }
        assert _norm(raw) == [{
            "kind": "assistant_step",
            "text": "PONG",
            "reasoning": "",
            "tool_uses": [],
        }]

    def test_thinking_block(self):
        raw = {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "let me think"}]},
        }
        assert _norm(raw) == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "let me think",
            "tool_uses": [],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_blocks": [
                    {"type": "thinking", "thinking": "let me think"},
                ],
            },
        }]

    def test_thinking_signature_is_metadata_not_reasoning_text(self):
        raw = {
            "type": "assistant",
            "message": {"content": [{
                "type": "thinking",
                "thinking": "consider the evidence",
                "signature": "sig-456",
            }]},
        }

        assert _norm(raw) == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "consider the evidence",
            "tool_uses": [],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_signatures": ["sig-456"],
                "native_signature": "sig-456",
                "native_blocks": [{
                    "type": "thinking",
                    "thinking": "consider the evidence",
                    "signature": "sig-456",
                }],
            },
        }]

    def test_multiple_thinking_blocks_keep_ordered_deduplicated_signatures(self):
        raw = {
            "type": "assistant",
            "message": {"content": [
                {
                    "type": "thinking",
                    "thinking": "first",
                    "signature": "sig-2",
                },
                {
                    "type": "thinking",
                    "thinking": "second",
                    "signature": "sig-1",
                },
                {
                    "type": "thinking",
                    "thinking": "duplicate",
                    "signature": "sig-2",
                },
            ]},
        }

        assert _norm(raw) == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "first\nsecond\nduplicate",
            "tool_uses": [],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_signatures": ["sig-2", "sig-1"],
                "native_blocks": [
                    {"type": "thinking", "thinking": "first", "signature": "sig-2"},
                    {"type": "thinking", "thinking": "second", "signature": "sig-1"},
                    {"type": "thinking", "thinking": "duplicate", "signature": "sig-2"},
                ],
            },
        }]

    def test_assistant_envelope_preserves_exact_model_and_complete_typed_blocks(self):
        raw = {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "first",
                        "signature": "sig-2",
                    },
                    {
                        "type": "redacted_thinking",
                        "data": "redacted-provider-payload",
                    },
                    {
                        "type": "thinking",
                        "thinking": "second",
                        "signature": "sig-1",
                    },
                ],
            },
        }

        assert _norm(raw) == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "first\nsecond",
            "tool_uses": [],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "source_effective_model": "claude-fable-5",
                "representation_kind": "anthropic_thinking",
                "native_signatures": ["sig-2", "sig-1"],
                "native_blocks": [
                    {"type": "thinking", "thinking": "first", "signature": "sig-2"},
                    {"type": "redacted_thinking", "data": "redacted-provider-payload"},
                    {"type": "thinking", "thinking": "second", "signature": "sig-1"},
                ],
            },
        }]

    def test_tool_use_block(self):
        raw = {
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "toolu_01ABC", "name": "Write",
                "input": {"file_path": "/tmp/x.txt", "content": "hi"},
            }]},
        }
        out = _norm(raw)
        assert out == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "",
            "tool_uses": [{
                "id": "toolu_01ABC", "name": "Write",
                "input": {"file_path": "/tmp/x.txt", "content": "hi"},
            }],
        }]

    def test_multi_block_order(self):
        raw = {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "plan"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            ]},
        }
        out = _norm(raw)
        assert out == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "plan",
            "tool_uses": [{
                "id": "t1", "name": "Bash", "input": {"command": "ls"},
            }],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_blocks": [
                    {"type": "thinking", "thinking": "plan"},
                ],
            },
        }]

    def test_mixed_text_and_tool_batch_is_one_assistant_step(self):
        raw = {
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "plan"},
                {"type": "text", "text": "I will inspect both files."},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/tmp/a"}},
                {"type": "tool_use", "id": "t2", "name": "Read",
                 "input": {"file_path": "/tmp/b"}},
            ]},
        }

        assert _norm(raw) == [{
            "kind": "assistant_step",
            "text": "I will inspect both files.",
            "reasoning": "plan",
            "tool_uses": [
                {"id": "t1", "name": "Read", "input": {"file_path": "/tmp/a"}},
                {"id": "t2", "name": "Read", "input": {"file_path": "/tmp/b"}},
            ],
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_blocks": [
                    {"type": "thinking", "thinking": "plan"},
                ],
            },
        }]


class TestToolResult:
    def test_string_content(self):
        raw = {
            "type": "user",
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "toolu_01ABC",
                "is_error": None, "content": "File created successfully",
            }]},
        }
        assert _norm(raw) == [{
            "kind": "tool_result", "tool_use_id": "toolu_01ABC",
            "content": "File created successfully", "is_error": False,
        }]

    def test_list_content_joined(self):
        raw = {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "t2", "is_error": True,
                "content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}],
            }]},
        }
        out = _norm(raw)
        assert out[0]["content"] == "line1\nline2"
        assert out[0]["is_error"] is True

    def test_image_block_becomes_placeholder(self):
        raw = {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "t3", "is_error": None,
                "content": [{"type": "image", "source": {
                    "type": "base64", "data": "iVBORw0KGgo...",
                    "media_type": "image/png",
                }}],
            }]},
        }
        out = _norm(raw)
        assert out[0]["content"] == "[image: image/png]"

    def test_mixed_text_and_image_blocks(self):
        raw = {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "t4", "is_error": None,
                "content": [
                    {"type": "text", "text": "caption"},
                    {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                ],
            }]},
        }
        assert _norm(raw)[0]["content"] == "caption\n[image: image]"


class TestResult:
    def test_success(self):
        raw = {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "DONE", "total_cost_usd": 0.1497, "num_turns": 1,
            "stop_reason": "end_turn", "session_id": "x",
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 200, "output_tokens": 7},
            "modelUsage": {"claude-opus-4-8[1m]": {"contextWindow": 1000000}},
        }
        out = _norm(raw)
        assert len(out) == 1
        ev = out[0]
        assert ev["kind"] == "result" and ev["result"] == "DONE"
        assert ev["cost_usd"] == 0.1497
        assert ev["aggregate_usage"]["cache_read_input_tokens"] == 100
        assert "usage" not in ev
        assert ev["model_usage"]["claude-opus-4-8[1m]"]["contextWindow"] == 1000000

    def test_success_without_usage(self):
        out = _norm({"type": "result", "subtype": "success", "is_error": False,
                     "result": "ok", "total_cost_usd": 0.0, "num_turns": 1,
                     "stop_reason": "end_turn"})
        assert out[0]["aggregate_usage"] is None and out[0]["model_usage"] is None


class TestRobustness:
    def test_unknown_type(self):
        assert _norm({"type": "totally_unknown"}) == []

    def test_assistant_empty_content(self):
        assert _norm({"type": "assistant", "message": {"content": []}}) == []

    def test_missing_message_key(self):
        assert _norm({"type": "assistant"}) == []

    def test_empty_dict(self):
        assert _norm({}) == []

    def test_malformed_assistant_text_and_thinking_values_are_ignored(self):
        assert _norm({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": None},
                {"type": "thinking", "thinking": {"bad": True}},
            ]},
        }) == []

    def test_non_mapping_tool_input_is_sanitized(self):
        assert _norm({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-1", "name": "Read",
                "input": ["not", "an", "object"],
            }]},
        }) == [{
            "kind": "assistant_step",
            "text": "",
            "reasoning": "",
            "tool_uses": [{
                "id": "tool-1",
                "name": "Read",
                "input": {},
                "malformed": True,
            }],
        }]

    @pytest.mark.parametrize("raw", [
        {"type": "assistant", "message": 1},
        {"type": "assistant", "message": {"content": 1}},
        {"type": "user", "message": ["bad"]},
        {"type": "user", "message": {"content": 1}},
        {"type": "stream_event", "event": 1},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": 1,
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": None},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": None},
        }},
    ])
    def test_malformed_nested_containers_are_ignored(self, raw):
        assert _norm(raw) == []

    def test_malformed_tool_result_blocks_are_total(self):
        out = _norm({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": [
                    {"type": "text", "text": None},
                    {"type": "image", "source": ["bad"]},
                ],
            }]},
        })
        assert out == [{
            "kind": "tool_result",
            "tool_use_id": "tool-1",
            "content": "[image: image]",
            "is_error": False,
        }]
