"""A confined worker's outside-content transcript stays in that worker."""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from infra.thread_confinement import Reader

from delegation_fixtures import deleg_db  # noqa: F401
from checkpoint_retrieval_fixtures import _install_checkpoint, fresh_db  # noqa: F401
from tests.child_parent_notice_helpers import db as notice_db, delegated, wakes  # noqa: F401

CANARY = "INJECT_CANARY_7f3e"
LABEL = "from an untrusted agent; contains outside content"


def seed(db, *, status="running", confinement="untrusted"):
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,confinement,confined_read_dirs,created_at,updated_at) "
        "VALUES ('isolated','u-test','Private title','native/test',?, '[\"/tmp\"]','t','t')",
        (confinement,),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('isolated-turn','isolated',0,'native/test',?,'2026-01-01','boot')", (status,),
    )
    digest = hashlib.sha256(CANARY.encode()).hexdigest()
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('tool-row','isolated','tool',?,?, '2026-01-01',1)",
        (CANARY, json.dumps({"turn_id": "isolated-turn", "name": "read_file",
                             "tool_result_sha256": digest, "tool_result_byte_count": len(CANARY)})),
    )
    db.commit()
    return digest


def add_final(db, text=CANARY):
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('final-row','isolated','assistant',?,?, '2026-01-02',2)",
        (text, json.dumps({"turn_id": "isolated-turn"})),
    )
    db.commit()


@pytest.mark.asyncio
async def test_status_history_and_scoped_result(deleg_db, monkeypatch):
    from tools import delegation
    from tools.query_projects import GetThreadHistoryTool
    from tools.tool_results import ReadToolResultTool

    digest = seed(deleg_db)
    monkeypatch.setattr(delegation, "run_in_db", AsyncMock(side_effect=lambda f, *a: f(*a)))
    status = json.loads(await delegation.DelegateStatusTool().execute("isolated", _thread_id="sender-self"))
    assert status["recent_messages"] == []
    assert status["title"] is None
    assert CANARY not in await GetThreadHistoryTool().execute("isolated", _thread_id="sender-self", tool_output="full")
    assert CANARY in await GetThreadHistoryTool().execute("isolated", _thread_id="isolated", tool_output="full")
    result = await ReadToolResultTool().execute(message_id="tool-row", sha256=digest,
                                                offset_bytes=0, limit_bytes=100, _thread_id="sender-self")
    assert result.startswith("Error:") and CANARY not in result
    add_final(deleg_db)
    deleg_db.execute("UPDATE active_turns SET status='completed' WHERE id='isolated-turn'")
    deleg_db.commit()
    status = json.loads(await delegation.DelegateStatusTool().execute("isolated", _thread_id="sender-self"))
    assert len(status["recent_messages"]) == 1
    assert LABEL in status["recent_messages"][0]["content"]
    assert CANARY in status["recent_messages"][0]["content"]


@pytest.mark.parametrize("with_fts", [False, True])
def test_search_filters_before_limit_and_self_can_read(deleg_db, with_fts):
    from tools.query_helpers import search_messages_fts

    seed(deleg_db)
    deleg_db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('allowed','u-test','OK','t','t')")
    deleg_db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,seq) VALUES ('ok','allowed','tool',?,'2025',1)", (CANARY,))
    deleg_db.execute("DROP TABLE IF EXISTS messages_fts")
    if with_fts:
        deleg_db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
        deleg_db.execute("INSERT INTO messages_fts(rowid, content) SELECT rowid, content FROM messages")
    deleg_db.commit()
    rows = search_messages_fts(deleg_db, CANARY, limit=1, reader=Reader.agent("sender-self"))
    assert [row["id"] for row in rows] == ["ok"]
    rows = search_messages_fts(deleg_db, CANARY, limit=10, reader=Reader.agent("isolated"))
    assert {row["id"] for row in rows} == {"ok", "tool-row"}


def test_finished_result_rejects_intermediate_and_running(deleg_db):
    from infra.thread_confinement import ChildOutput

    seed(deleg_db)
    add_final(deleg_db)
    child = ChildOutput.read(deleg_db, "isolated")
    assert child.confined and child.answer(deleg_db) is None
    deleg_db.execute("UPDATE active_turns SET status='completed' WHERE id='isolated-turn'")
    assert child.answer(deleg_db)["content"] == CANARY
    deleg_db.execute("UPDATE messages SET metadata=? WHERE id='final-row'", (json.dumps({"turn_id": "isolated-turn", "llm_tool_calls": [{"id": "x"}]}),))
    assert child.answer(deleg_db) is None


@pytest.mark.asyncio
async def test_api_status_and_messages_keep_person_view(deleg_db, monkeypatch):
    from api.delegation import thread_status
    from api.thread_messages import get_messages, get_tool_result
    from infra.internal_principal import internal_request

    seed(deleg_db)
    with internal_request("u-test", "sender-self"):
        status = await thread_status("isolated", token="ignored", messages=5)
        assert status["recent_messages"] == []
        page = await get_messages("isolated", token="ignored")
        assert CANARY not in json.dumps(page)
        result = await get_tool_result("isolated", "tool-row", "call", token="ignored")
        assert "restricted" in result
    with internal_request("u-test", "isolated"):
        status = await thread_status("isolated", token="ignored", messages=5)
        assert CANARY in json.dumps(status["recent_messages"])
        self_page = await get_messages("isolated", token="ignored")
    import api.delegation as delegation_api
    import api.thread_messages as messages_api

    session = lambda _token: {"id": "u-test", "token_kind": "session"}
    with monkeypatch.context() as patch:
        patch.setattr(delegation_api, "authenticate", session)
        patch.setattr(messages_api, "authenticate", session)
        status = await thread_status("isolated", token="person", messages=5)
        page = await get_messages("isolated", token="person")
        assert CANARY in json.dumps(status["recent_messages"])
        assert page == self_page
    add_final(deleg_db)
    deleg_db.execute("UPDATE active_turns SET status='completed' WHERE id='isolated-turn'")
    deleg_db.commit()
    with internal_request("u-test", "sender-self"):
        status = await thread_status("isolated", token="ignored", messages=5)
        assert LABEL in status["recent_messages"][0]["content"]
        assert CANARY in status["recent_messages"][0]["content"]


def test_checkpoint_search_and_reads_exclude_confined_child(fresh_db):
    from memory.checkpoint_search import get_checkpoint, recent_checkpoints, search_checkpoints

    fresh_db.execute("UPDATE threads SET confinement='untrusted' WHERE id='t1'")
    fresh_db.execute("INSERT INTO threads (id,user_id,title) SELECT 'parent',user_id,'Parent' FROM threads WHERE id='t1'")
    checkpoint = _install_checkpoint(fresh_db, thread_id="t1", narrative=CANARY)
    assert search_checkpoints(fresh_db, CANARY, reader=Reader.agent("parent")) == []
    _install_checkpoint(fresh_db, thread_id="parent", narrative=CANARY)
    matches = search_checkpoints(fresh_db, CANARY, reader=Reader.agent("parent"), limit=1)
    assert len(matches) == 1 and matches[0]["thread_id"] == "parent"
    assert recent_checkpoints(fresh_db, "t1", reader=Reader.agent("parent")) == []
    assert CANARY not in json.dumps(get_checkpoint(fresh_db, checkpoint, reader=Reader.agent("parent")))
    assert CANARY in json.dumps(get_checkpoint(fresh_db, checkpoint, reader=Reader.agent("t1")))


def test_parent_completion_and_plan_return_label_finished_result(deleg_db):
    from infra.thread_driver import _parent_completion
    from plans.delegation_turn_end import turn_reply

    seed(deleg_db, status="completed")
    deleg_db.execute("UPDATE threads SET spawned_by='sender-self' WHERE id='isolated'")
    assert _parent_completion(deleg_db, "isolated-turn", "isolated") is None
    assert turn_reply(deleg_db, "isolated") is None
    add_final(deleg_db)
    assert _parent_completion(deleg_db, "isolated-turn", "isolated") == "sender-self"
    row = deleg_db.execute("SELECT content FROM messages WHERE id='parent-completion:isolated-turn'").fetchone()
    assert LABEL in row[0] and CANARY in row[0]
    reply = turn_reply(deleg_db, "isolated")
    assert LABEL in reply["text"] and CANARY in reply["text"]


def test_error_notice_does_not_expose_worker_error(notice_db):
    from plans.delegation_child_lifecycle import notify_turn_error

    delegated(notice_db)
    notice_db.execute("UPDATE threads SET confinement='untrusted' WHERE id='worker'")
    notify_turn_error(notice_db, "worker", error=CANARY)
    assert CANARY not in json.dumps([dict(row) for row in wakes(notice_db)])
    assert "Couldn't finish this untrusted agent's turn." in str(wakes(notice_db)[0]["content"])


@pytest.mark.asyncio
async def test_untrusted_thread_is_never_auto_titled_and_title_withheld_at_read(deleg_db, monkeypatch):
    from infra.thread_confinement import project_thread
    from ws import post_turn

    seed(deleg_db)
    deleg_db.execute("UPDATE threads SET title='New Chat' WHERE id='isolated'")
    deleg_db.execute("INSERT INTO messages (id,thread_id,role,content,created_at) VALUES ('title-canary','isolated','assistant',?,?)", (CANARY, '2026-01-01'))
    deleg_db.commit()
    title_llm = AsyncMock(return_value=CANARY)
    monkeypatch.setattr(post_turn, "generate_thread_title", title_llm)
    await post_turn.ensure_thread_title("isolated", AsyncMock())
    row = deleg_db.execute("SELECT * FROM threads WHERE id='isolated'").fetchone()
    title_llm.assert_not_called()
    assert project_thread(Reader.person(), row)["title"] == "New Chat"
    assert project_thread(Reader.agent("sender-self"), row)["title"] is None


def test_unconfined_agent_read_is_unchanged():
    from infra.thread_confinement import may_read_transcript

    thread = {"id": "child", "confinement": "untrusted"}
    assert may_read_transcript(Reader.agent("parent"), {**thread, "confinement": None})
    assert may_read_transcript(Reader.person(), thread)
    assert may_read_transcript(Reader.agent("child"), thread)
    assert not may_read_transcript(Reader.agent("parent"), thread)
    assert not may_read_transcript(Reader.from_tool({}), thread)
    assert not may_read_transcript(Reader.from_user({"token_kind": "internal"}), thread)
