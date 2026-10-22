"""An undispatched same-Step echo refuses once a sibling has a result."""
from __future__ import annotations

import pytest

from core.fork_types import ForkNotAdmitted
from test_cold_install_floor_debt import _append_candidate, _capture_and_install, _request
from test_external_cold_install_race import _codex_events, _event
from test_external_cold_tool_debt import _context, debt_db  # noqa: F401


@pytest.mark.asyncio
async def test_codex_refused_sibling_blocks_cold_install_until_missing_result_settles(
    debt_db, monkeypatch,
) -> None:
    """The refused Codex sibling itself blocks cold work until it is settled."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from subagents.codex_runtime_events import settle_missing_tool_results
    from subagents.harness_checkpoint_cold_admission import build_external_cold_admission

    context = _context(debt_db, monkeypatch, "codex", outstanding=False)
    live, on_event = _codex_events(debt_db)
    canonical_older = debt_db.execute(
        "SELECT content, metadata FROM messages WHERE id='older-result'"
    ).fetchone()

    await on_event(_event("tool_use", "refused"))
    assert live.tool_step_ids == {"refused": "older-step"}
    with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
        await on_event(_event("tool_result", "refused"))
    assert live.tool_step_ids == {"refused": "older-step"}
    assert debt_db.execute(
        "SELECT count(*) FROM messages WHERE thread_id='debt' AND role='tool' "
        "AND json_extract(metadata, '$.tool_call_id')='refused'"
    ).fetchone()[0] == 0

    with pytest.raises(ForkNotAdmitted, match="tool calls are outstanding"):
        build_external_cold_admission(
            "blocked", context=context, live=live, identity=live.corsia_turn,
        )

    candidate = _capture_and_install(
        debt_db,
        _request(checkpoint_id="candidate", through_seq=_append_candidate(debt_db)),
    )
    assert candidate.status == "overtaken"
    assert "tool" in candidate.error
    from infra import checkpoint_queries

    assert checkpoint_queries.current_installed("debt", db=debt_db) is None

    await settle_missing_tool_results(
        live=live, thread_id="debt", identity=live.corsia_turn, bcast=lambda *_args: None,
    )

    assert live.tool_step_ids == {}
    assert live.tools_settled.is_set()
    refused = debt_db.execute(
        "SELECT content, metadata FROM messages WHERE thread_id='debt' "
        "AND role='tool' AND json_extract(metadata, '$.tool_call_id')='refused'"
    ).fetchone()
    assert refused["content"] == (
        "Codex did not provide a result for this tool call. "
        "CORSIA will rebuild provider context before the next turn."
    )
    assert '"synthetic_reason": "provider_missing_result"' in refused["metadata"]
    assert debt_db.execute(
        "SELECT count(*) FROM messages WHERE thread_id='debt' AND role='tool' "
        "AND json_extract(metadata, '$.tool_call_id')='refused'"
    ).fetchone()[0] == 1
    assert debt_db.execute(
        "SELECT content, metadata FROM messages WHERE id='older-result'"
    ).fetchone() == canonical_older
