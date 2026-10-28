"""Malformed invocation provenance must never authorize an undispatched row."""
import pytest

from test_tool_result_same_step_binding import _seed_thread


@pytest.mark.parametrize("reference", [
    "{broken", "[]", '{"invocation_id":""}',
    '{"invocation_id":"   "}', '{"invocation_id":42}',
])
def test_malformed_marker_with_bound_sibling_refuses_fallback(isolated_db, reference):
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    with get_db() as db:
        _seed_thread(db)
        step = persist_tool_use(
            "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
            tool_id="first", name="write_file", arguments={}, db=db,
        )
        bind_tool_result_echo(
            thread_id="thread-1", provider_tool_call_id="first",
            assistant_step_id=step, echoed_content="undispatched", db=db,
        )
        assert persist_tool_use(
            "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
            tool_id="second", name="write_file", arguments={}, assistant_step_id=step, db=db,
        ) == step
        before = db.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0]
        with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
            bind_tool_result_echo(
                thread_id="thread-1", provider_tool_call_id="second",
                assistant_step_id=step,
                echoed_content=f"echo\n\n[CORSIA_TOOL_INVOCATION reference={reference}]", db=db,
            )
        assert db.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0] == before
