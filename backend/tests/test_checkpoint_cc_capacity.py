"""Claude checkpoint replies wait for the complete current-call measurement."""

from dataclasses import replace

from test_checkpoint_external_adapters import _target


def _context(window=1000):
    from core.checkpoint_cc_context import CCCheckpointContext

    target = replace(_target("claude-code"), context_window=window,
                     transport_model="gpt-4", expected_reported_model="gpt-4")
    return CCCheckpointContext(target, 10)


def _stream(context, event):
    context.observe({"type": "stream_event", "event": event})


def test_claude_waits_for_complete_output_and_counts_disjoint_cache_tokens():
    context = _context()
    _stream(context, {"type": "message_start", "message": {"id": "m1", "usage": {
        "input_tokens": 50, "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 100, "output_tokens": 1,
    }}})
    _stream(context, {"type": "content_block_start", "content_block": {
        "type": "tool_use", "id": "edit", "name": "edit", "input": {},
    }})
    assert not context.ready("edit")
    _stream(context, {"type": "message_delta", "usage": {"output_tokens": 100}})
    _stream(context, {"type": "message_stop"})
    assert context.ready("edit")
    assert context.tokens == 950
    assert context.accept_tool_result(" x" * 20)
    assert not context.accept_tool_result(" x" * 40)


def test_next_call_replaces_the_previous_count_without_rebilling_its_tool_results():
    context = _context()
    for message_id, input_tokens in (("first", 100), ("second", 130)):
        _stream(context, {"type": "message_start", "message": {
            "id": message_id, "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        }})
        _stream(context, {"type": "message_delta", "usage": {"output_tokens": 10}})
        _stream(context, {"type": "message_stop"})
        assert context.tokens == input_tokens + 10
        assert context.accept_tool_result(" x" * 20)


def test_foreign_or_unseen_call_is_not_admitted_by_an_earlier_completed_call():
    context = _context()
    _stream(context, {"type": "message_start", "message": {
        "id": "first", "usage": {"input_tokens": 100, "output_tokens": 0},
    }})
    _stream(context, {"type": "message_stop"})
    assert not context.ready("unseen")


def test_initial_partial_output_count_is_not_a_final_response_measurement():
    context = _context()
    _stream(context, {"type": "message_start", "message": {
        "id": "first", "usage": {"input_tokens": 100, "output_tokens": 1},
    }})
    _stream(context, {"type": "content_block_start", "content_block": {
        "type": "tool_use", "id": "edit",
    }})
    _stream(context, {"type": "message_stop"})
    assert not context.ready("edit")
