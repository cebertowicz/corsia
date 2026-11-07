"""Design row R3: an agent outside a project cannot read its chat history or checkpoints.

A message or checkpoint row records no project, so a history read fences on the
thread it belongs to: that thread's current project, or its cut once it left a
Local only project. Every reader here must answer from ``infra.project_fence``,
the same rule the file gate uses, so a history read and a file read under the
same caller never disagree.
"""

from __future__ import annotations

import json

import pytest

USER = "u"
P_ALPHA = "p-alpha"
P_BETA = "p-beta"
P_SECRET = "p-secret"
T_ALPHA = "t-alpha"
T_BETA = "t-beta"
T_SECRET = "t-secret"
T_OPEN = "t-open"
CALLER_ALPHA = "caller-alpha"
CALLER_BETA = "caller-beta"
CALLER_SECRET = "caller-secret"
CORSIA_AGENT = "corsia-agent"
SECRET_CONDUCTOR = "secret-conductor"


def _sql(statement: str, params: tuple = ()) -> None:
    """Write through the pooled connection the tools under test also use."""
    from infra.database import get_db

    db = get_db()
    db.execute(statement, params)
    db.commit()


def _project(project_id: str, name: str, *, conductor: str | None = None) -> None:
    _sql(
        "INSERT INTO projects(id,name,conductor_thread_id,created_at,updated_at)"
        " VALUES (?,?,?,'t','t')",
        (project_id, name, conductor),
    )


def _thread(
    thread_id: str,
    *,
    project: str | None = None,
    local_only: bool = False,
    agent_enabled: bool = True,
) -> None:
    _sql(
        "INSERT INTO threads(id,user_id,model,title,project_id,local_only,agent_enabled,"
        "created_at,updated_at) VALUES (?,?,'m',?,?,?,?,'t','t')",
        (thread_id, USER, thread_id, project, 1 if local_only else 0, 1 if agent_enabled else 0),
    )


def _message(thread_id: str, seq: int, content: str) -> None:
    _sql(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,delivery_state,seq)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            f"{thread_id}-m{seq}", thread_id, "user", content, "{}",
            f"2026-11-0{seq}T00:00:00", "delivered", seq,
        ),
    )


def _checkpoint(thread_id: str, *, narrative: str, continuity: list[str] | None = None) -> None:
    """Install a checkpoint the way capture and install leave it, index included.

    The shared helper indexes before it writes the continuity rows, so the entry
    index is rebuilt here once those rows exist, as install leaves them.
    """
    from checkpoint_retrieval_fixtures import _install_checkpoint
    from infra.checkpoint_entry_index import index_checkpoint_entries

    db = _db()
    cid = _install_checkpoint(
        db, thread_id=thread_id, narrative=narrative, continuity=continuity or []
    )
    if continuity:
        index_checkpoint_entries(db, cid)
    db.commit()
    return cid


def _move(thread_id: str, *, to_project: str | None, from_project: str | None) -> None:
    """Move a thread through the API writer, the same call the app makes."""
    from api.thread_mutations import _write_project_patch
    from infra.time import utc_now_iso

    _write_project_patch(_db(), thread_id, to_project, from_project, utc_now_iso())


def _db():
    from infra.database import get_db

    return get_db()


@pytest.fixture
def world(isolated_db, monkeypatch):  # noqa: ARG001 - isolated_db builds the schema
    """Three projects and one caller inside each of the two ordinary ones."""
    from infra.db_fts import _init_checkpoint_entry_search_fts, _init_checkpoints_fts

    _project(P_ALPHA, "alpha")
    _project(P_BETA, "beta")
    _project(P_SECRET, "secret", conductor=SECRET_CONDUCTOR)
    _sql(
        "INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES (?,1)",
        (P_SECRET,),
    )
    for thread, project in (
        (T_ALPHA, P_ALPHA), (T_BETA, P_BETA), (T_SECRET, P_SECRET), (T_OPEN, None),
        (CALLER_ALPHA, P_ALPHA), (CALLER_BETA, P_BETA), (CALLER_SECRET, P_SECRET),
        (CORSIA_AGENT, None),
    ):
        _thread(thread, project=project)
    _sql(
        "INSERT OR REPLACE INTO settings(key,value,updated_at)"
        " VALUES ('corsia_agent_thread_id',?,'t')",
        (json.dumps({"session_id": CORSIA_AGENT}),),
    )
    # The checkpoint half of a search reads the two FTS indexes startup builds.
    db = _db()
    _init_checkpoints_fts(db)
    _init_checkpoint_entry_search_fts(db)
    db.commit()
    # Tools hand their job to the DB pool; run it inline on this connection.
    for module in ("tools.query_projects", "tools.query_search", "tools.query_checkpoints",
                   "tools.delegation"):
        monkeypatch.setattr(f"{module}.run_in_db", _inline)
    return db


async def _inline(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def _history(thread_id: str, *, caller: str | None = None, **kwargs) -> str:
    from tools.query_projects import GetThreadHistoryTool

    return await GetThreadHistoryTool().execute(thread_id, **_caller(caller), **kwargs)


async def _search(query: str, *, caller: str | None = None, **kwargs) -> str:
    from tools.query_search import SearchHistoryTool

    return await SearchHistoryTool().execute(query, **_caller(caller), **kwargs)


async def _checkpoints(thread_id: str, *, caller: str | None = None, **kwargs) -> str:
    from tools.query_checkpoints import GetCheckpointsTool

    return await GetCheckpointsTool().execute(thread_id=thread_id, **_caller(caller), **kwargs)


def _caller(caller: str | None) -> dict:
    """The backend-injected caller; no thread is the person's own session."""
    return {"_thread_id": caller, "_user_id": USER} if caller else {"_user_id": USER}


async def test_agent_reads_the_history_of_its_own_project(world):
    """The wall narrows reads across projects only: same-project history is unchanged."""
    _message(T_ALPHA, 1, "keep the plan")

    out = await _history(T_ALPHA, caller=CALLER_ALPHA)

    assert "keep the plan" in out


async def test_agent_outside_a_project_is_refused_its_thread_history(world):
    """A direct read naming a fenced thread refuses with the thread copy: same wall, no
    offer of a host-access lease, which cannot open history."""
    _message(T_BETA, 1, "beta private decision")

    out = await _history(T_BETA, caller=CALLER_ALPHA)

    assert f"`{T_BETA}` is in project `beta`, outside your scope" in out
    assert "Host access doesn't open it. Ask the CORSIA agent." in out
    assert "request_host_access" not in out
    assert "beta private decision" not in out


async def test_local_only_project_refusal_names_its_conductor(world):
    """A Local only project uses the Local only copy, which says host access will not help."""
    _message(T_SECRET, 1, "on-machine only")

    out = await _history(T_SECRET, caller=CALLER_ALPHA)

    assert f"`{T_SECRET}` is in Local only project `secret`, outside your scope" in out
    assert SECRET_CONDUCTOR in out
    assert "on-machine only" not in out


async def test_persons_session_reads_every_projects_history(world):
    """The person's own session carries no thread, so it is fenced from nothing."""
    _message(T_SECRET, 1, "on-machine only")
    _message(T_BETA, 1, "beta private decision")

    assert "on-machine only" in await _history(T_SECRET)
    assert "beta private decision" in await _history(T_BETA)


async def test_unknown_caller_fails_closed(world):
    """A caller whose thread cannot be read is fenced from every project."""
    _message(T_BETA, 1, "beta private decision")

    out = await _history(T_BETA, caller="no-such-thread")

    assert "outside your scope" in out
    assert "request_host_access" not in out
    assert "beta private decision" not in out


async def test_fail_closed_caller_on_a_thread_with_no_project_names_the_workspace_agent(world):
    """The workspace-agent thread copy is reachable, not decoration: a caller whose own
    thread row cannot be read is fenced from everything, so a thread that belongs to no
    project is refused too, and the refusal names what it is."""
    _message(T_OPEN, 1, "workspace-level decision")

    out = await _history(T_OPEN, caller="no-such-thread")

    assert f"`{T_OPEN}` belongs to a workspace agent, outside your scope" in out
    assert "Host access doesn't open it. Ask the CORSIA agent." in out
    assert "request_host_access" not in out
    assert "workspace-level decision" not in out


async def test_corsia_agent_is_walled_from_local_only_history_only(world):
    """The workspace agent keeps ordinary project history and loses only Local only."""
    _message(T_SECRET, 1, "on-machine only")
    _message(T_BETA, 1, "beta private decision")

    refused = await _history(T_SECRET, caller=CORSIA_AGENT)
    assert "on-machine only" not in refused
    assert "Local only project `secret`" in refused
    assert "beta private decision" in await _history(T_BETA, caller=CORSIA_AGENT)


async def test_a_locked_local_only_corsia_agent_is_walled_from_nothing(world):
    """On a local model the workspace agent is inside every project, so it reads Local only."""
    _message(T_SECRET, 1, "on-machine only")
    _sql("UPDATE threads SET local_only = 1 WHERE id = ?", (CORSIA_AGENT,))

    assert "on-machine only" in await _history(T_SECRET, caller=CORSIA_AGENT)


async def test_messages_at_or_below_the_cut_are_fenced_from_the_new_project(world):
    """Leaving a Local only project cuts the thread's earlier messages for everyone,
    including the project it moved into."""
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_BETA))
    for seq in (1, 2, 3):
        _message(T_BETA, seq, f"secret-era note {seq}")
    _move(T_BETA, to_project=P_BETA, from_project=P_SECRET)
    _message(T_BETA, 4, "after the move")

    out = await _history(T_BETA, caller=CALLER_BETA)

    assert "after the move" in out
    for seq in (1, 2, 3):
        assert f"secret-era note {seq}" not in out


async def test_both_move_writers_record_the_cut(world):
    """Each writer sets the cut in its own transaction, and a second leave keeps the later one."""
    def cut() -> int | None:
        return _db().execute(
            "SELECT local_only_cut_seq FROM threads WHERE id = ?", (T_ALPHA,)
        ).fetchone()["local_only_cut_seq"]

    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_ALPHA))
    for seq in (1, 2):
        _message(T_ALPHA, seq, f"secret-era note {seq}")
    _move(T_ALPHA, to_project=P_ALPHA, from_project=P_SECRET)
    assert cut() == 2

    # Back into a Local only project, then out of it through the other writer.
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_ALPHA))
    for seq in (3, 4):
        _message(T_ALPHA, seq, f"second secret-era note {seq}")
    from tools.query_projects import SetConversationProjectTool

    await SetConversationProjectTool().execute(T_ALPHA, project_id=P_BETA, **_caller(None))
    assert cut() == 4


async def test_leaving_an_ordinary_project_writes_no_cut(world):
    """An ordinary project needs no cut: its history is fenced by the current-project rule."""
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_ALPHA, T_BETA))
    _message(T_BETA, 1, "alpha-era note")
    _move(T_BETA, to_project=P_BETA, from_project=P_ALPHA)

    assert _db().execute(
        "SELECT local_only_cut_seq FROM threads WHERE id = ?", (T_BETA,)
    ).fetchone()["local_only_cut_seq"] is None
    assert "alpha-era note" in await _history(T_BETA, caller=CALLER_BETA)


async def test_every_checkpoint_of_a_cut_thread_is_fenced(world):
    """A checkpoint carries earlier continuity forward whatever its own sequence, so a
    thread with a cut has no readable checkpoint at all, before or after the move."""
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_BETA))
    _message(T_BETA, 1, "secret-era note")
    _checkpoint(T_BETA, narrative="We chose event routing.")
    _move(T_BETA, to_project=P_BETA, from_project=P_SECRET)
    _checkpoint(T_BETA, narrative="Carried the pre-move decision forward.")

    out = await _checkpoints(T_BETA, caller=CALLER_BETA)

    assert "event routing" not in out
    assert "pre-move" not in out
    assert "Carried" not in out


async def test_search_returns_no_checkpoint_of_a_fenced_thread(world):
    """``include_checkpoints`` may not read through the wall either."""
    _checkpoint(T_SECRET, narrative="We chose event routing.")

    # The same query from inside the project answers, so the empty result below is
    # the wall and not a search that never matched.
    assert "event routing" in await _search(
        "routing", caller=CALLER_SECRET, include_checkpoints="true", limit=10
    )
    assert "event routing" not in await _search(
        "routing", caller=CALLER_ALPHA, include_checkpoints="true", limit=10
    )


async def test_checkpoint_entry_search_drops_a_fenced_threads_entry(world):
    """The entry-level index is fenced too, so a carried continuity entry stays inside."""
    _checkpoint(
        T_SECRET,
        narrative="narrative line",
        continuity=[{"statement": "we chose event routing for deliveries"}],
    )
    assert "event routing" in await _search(
        "routing", caller=CALLER_SECRET, include_checkpoints="true", limit=10
    )
    assert "event routing" not in await _search(
        "routing", caller=CALLER_ALPHA, include_checkpoints="true", limit=10
    )


async def test_unscoped_search_carries_the_fixed_notice_even_when_empty(world):
    """An agent that is walled from something is told so, with no count to measure from."""
    from infra.project_fence import HISTORY_NOTICE

    out = await _search("nothing matches this", caller=CALLER_ALPHA)

    assert HISTORY_NOTICE in out
    assert out.strip().endswith(HISTORY_NOTICE)


async def test_thread_scoped_search_carries_no_notice(world):
    """A search inside one named thread is refused or answered, never narrowed quietly."""
    from infra.project_fence import HISTORY_NOTICE

    _message(T_ALPHA, 1, "keep the plan")

    out = await _search("keep", caller=CALLER_ALPHA, thread_id=T_ALPHA)

    assert HISTORY_NOTICE not in out


async def test_delegate_status_refuses_a_fenced_thread(world):
    """Status of a walled thread is refused outright, so it carries no recent messages."""
    from tools.delegation import DelegateStatusTool

    _message(T_SECRET, 1, "on-machine only")

    out = await DelegateStatusTool().execute(
        T_SECRET, messages=5, _thread_id=CALLER_ALPHA, _user_id=USER
    )

    assert "on-machine only" not in out
    assert "outside your scope" in out


async def test_delegate_status_drops_messages_below_the_cut(world):
    """Recent messages of a thread with a cut start above the cut, for its own project."""
    from tools.delegation import DelegateStatusTool

    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_BETA))
    _message(T_BETA, 1, "secret-era note")
    _move(T_BETA, to_project=P_BETA, from_project=P_SECRET)
    _message(T_BETA, 2, "after the move")

    out = await DelegateStatusTool().execute(
        T_BETA, messages=5, _thread_id=CALLER_BETA, _user_id=USER
    )

    assert "after the move" in out
    assert "secret-era note" not in out


async def test_unknown_caller_reads_no_row_from_an_unscoped_search(world):
    """A caller that cannot be read is fenced from every project and from the workspace.

    The direct read refuses, so the SQL readers have to give the same answer: a clause
    that kept project rows and dropped the rest would be exactly backwards.
    """
    from infra.project_fence import HISTORY_NOTICE

    _message(T_BETA, 1, "beta routing decision")
    _message(T_OPEN, 1, "workspace routing decision")

    out = await _search("routing", caller="no-such-thread")

    assert "beta routing decision" not in out
    assert "workspace routing decision" not in out
    assert HISTORY_NOTICE in out


async def test_unknown_caller_reads_no_checkpoint_by_search_list_or_id(world):
    """The checkpoint half fails closed the same way, by search, by list and by id."""
    cid = _checkpoint(T_BETA, narrative="We chose event routing.", continuity=["beta held the line"])
    from tools.query_checkpoints import GetCheckpointsTool

    search = await _search("routing", caller="no-such-thread", include_checkpoints=True)
    listed = await _checkpoints(T_BETA, caller="no-such-thread")
    by_id = await GetCheckpointsTool().execute(checkpoint_id=cid, **_caller("no-such-thread"))
    control = await GetCheckpointsTool().execute(checkpoint_id=cid, **_caller(CALLER_BETA))

    assert "event routing" not in search
    assert "event routing" not in listed
    # A checkpoint id is answered with the generic unavailable text, the same as a
    # missing one: this read does not get to learn that the id exists elsewhere.
    assert "event routing" not in by_id
    assert "not_found" in by_id
    assert "event routing" in control


async def test_fenced_rows_do_not_consume_the_page_limit(world):
    """The wall runs in SQL, so a hidden row never takes one of the requested slots.

    Two shapes, because a requested page size is exactly where a fence applied
    after the query would hide in a passing test: a thread's own history, where
    the cut hides the older rows, and an unscoped search, where the walled
    project's rows are newer and would otherwise spend the whole page.
    """
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_BETA))
    for seq in (1, 2, 3):
        _message(T_BETA, seq, f"secret-era note {seq}")
    _move(T_BETA, to_project=P_BETA, from_project=P_SECRET)
    for seq in (4, 5):
        _message(T_BETA, seq, f"after the move {seq}")

    history = await _history(T_BETA, caller=CALLER_BETA, limit=4)

    assert "after the move 4" in history
    assert "after the move 5" in history
    assert "secret-era note" not in history

    for seq in (1, 2, 3, 4):
        _message(T_ALPHA, seq, f"ledger note alpha {seq}")
    for seq in (5, 6, 7, 8, 9):
        _message(T_SECRET, seq, f"ledger note secret {seq}")

    found = await _search("ledger", caller=CALLER_ALPHA, limit=4)

    assert all(f"ledger note alpha {seq}" in found for seq in (1, 2, 3, 4))
    assert "ledger note secret" not in found


async def test_message_search_drops_messages_below_the_cut(world):
    """The cut rule reaches the message search too, not only a thread's own history."""
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_BETA))
    _message(T_BETA, 1, "routingplan settled pre-move")
    _move(T_BETA, to_project=P_BETA, from_project=P_SECRET)
    _message(T_BETA, 2, "routingplan settled post-move")

    out = await _search("routingplan", caller=CALLER_BETA, thread_id=T_BETA)

    assert "routingplan settled post-move" in out
    assert "routingplan settled pre-move" not in out


async def test_the_fallback_search_applies_the_same_fence(world, monkeypatch):
    """When FTS5 cannot run the query, the LIKE fallback fences the same rows."""
    # An expression FTS5 rejects, which is what sends a real search down this path.
    monkeypatch.setattr("tools.query_helpers.fts5_escape", lambda query: "AND")
    _sql("UPDATE threads SET project_id = ? WHERE id = ?", (P_SECRET, T_BETA))
    _message(T_BETA, 1, "routingplan settled pre-move")
    _move(T_BETA, to_project=P_BETA, from_project=P_SECRET)
    _message(T_BETA, 2, "routingplan settled post-move")

    out = await _search("routingplan", caller=CALLER_BETA, thread_id=T_BETA)

    assert "routingplan settled post-move" in out
    assert "routingplan settled pre-move" not in out
