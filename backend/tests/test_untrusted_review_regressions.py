"""Regression cases for the untrusted transcript disclosure review."""
import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from checkpoint_retrieval_fixtures import _install_checkpoint, fresh_db  # noqa: F401
from tests.child_parent_notice_helpers import db as notice_db, delegated, wakes  # noqa: F401
from tests.test_untrusted_transcript_disclosure import add_final, seed


@pytest.mark.asyncio
async def test_restricted_history_and_api_explain_the_boundary(deleg_db):
    from api.thread_messages import get_messages, get_tool_result
    from infra.internal_principal import internal_request
    from tools.query_projects import GetThreadHistoryTool

    seed(deleg_db)
    expected = "This is an untrusted agent's thread. Only its final result is shared, through delegate_status."
    history = await GetThreadHistoryTool().execute("isolated", _thread_id="sender-self")
    assert expected in history
    with internal_request("u-test", "sender-self"):
        page = await get_messages("isolated", token="unused")
        result = await get_tool_result("isolated", "tool-row", "call", token="unused")
    assert page["restricted"] is True and page["restriction_reason"] == expected
    assert result["restricted"] is True and result["restriction_reason"] == expected


def test_untrusted_provider_refusal_keeps_classified_reason(notice_db):
    from plans.delegation_child_lifecycle import notify_turn_error

    delegated(notice_db)
    notice_db.execute("UPDATE threads SET confinement='untrusted' WHERE id='worker'")
    reason = "You've hit your session limit."
    notify_turn_error(notice_db, "worker", error=reason)
    [wake] = wakes(notice_db)
    assert str(wake["id"]).startswith("plan-wake:child_provider_refused:")
    assert reason in wake["content"]


def test_distinct_untrusted_errors_remain_distinct_wakes(notice_db):
    from plans.delegation_child_lifecycle import notify_turn_error

    delegated(notice_db)
    notice_db.execute("UPDATE threads SET confinement='untrusted' WHERE id='worker'")
    notify_turn_error(notice_db, "worker", error="first private error")
    notify_turn_error(notice_db, "worker", error="second private error")
    results = wakes(notice_db)
    assert len(results) == 2
    assert all("untrusted agent's turn failed" in row["content"] for row in results)
    assert "private error" not in json.dumps([dict(row) for row in results])


def test_checkpoint_entry_defaults_fail_closed(fresh_db):
    from memory.checkpoint_entry_rows import entry_rank_rows_matching, entry_rows_matching

    fresh_db.execute("UPDATE threads SET confinement='untrusted' WHERE id='t1'")
    _install_checkpoint(fresh_db, thread_id='t1', narrative='note', continuity=[{'statement': 'needle'}])
    for search in (entry_rank_rows_matching, entry_rows_matching):
        assert search(fresh_db, 'needle', 'u', None, 20, 3) == []
        assert search(fresh_db, 'needle', 'u', None, 20, 3, 't1')


def test_untrusted_plan_reply_keeps_tail_cap(deleg_db):
    from plans.delegation_turn_end import FINAL_REPLY_CHARS, turn_reply

    seed(deleg_db, status="completed")
    add_final(deleg_db, "x" * (FINAL_REPLY_CHARS + 100))
    text = turn_reply(deleg_db, "isolated")["text"]
    assert text == "from an untrusted agent; contains outside content\n\n…" + "x" * FINAL_REPLY_CHARS
