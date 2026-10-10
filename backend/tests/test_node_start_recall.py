"""Related past work at node start: what clears the floor, and what stays quiet.

The lookup is advisory and agent-visible, so the contract is as much about
silence as about hits: nothing below the floor, nothing the caller already
carries live, nothing another user owns, and never an error in its caller.
"""

from __future__ import annotations

import json

import pytest
from loguru import logger

from checkpoint_retrieval_fixtures import (  # noqa: F401
    OWNER_USER,
    _install_checkpoint,
    fresh_db,
)

#: Where a node of t1's plan points a caller in t2: plan_read cannot open it,
#: and no read returns the node, so the pointer promises only that thread's
#: messages (#2520).
ELSEWHERE = '→ get_thread_history(thread_id="t1") — that thread\'s messages, not this node'

IDLE_ROOT_CAUSE = (
    "Root cause of missed idle checkpoints: accepting a passive input destroyed "
    "the idle marker and cancelled its timer."
)


@pytest.fixture
def db(fresh_db):
    """Add a second owned thread and the plan rows the node half reads."""
    fresh_db.executescript(
        "ALTER TABLE threads ADD COLUMN purged_at TEXT;"
        "INSERT INTO threads (id,user_id,title,history_epoch) VALUES ('t2','u','T2',0);"
        "CREATE TABLE plans (id TEXT PRIMARY KEY, owner_kind TEXT, owner_id TEXT);"
        "CREATE TABLE plan_nodes (id TEXT PRIMARY KEY, plan_id TEXT, title TEXT,"
        " acceptance_criteria TEXT, description TEXT, state TEXT, updated_at TEXT,"
        " requirements_revision INTEGER);"
        "CREATE TABLE plan_node_events (node_id TEXT, event TEXT, payload_json TEXT,"
        " created_at TEXT);"
        "INSERT INTO plans VALUES ('p1','thread','t1'),('pf','thread','t-foreign');"
    )
    return fresh_db


def _node(db, node_id, title, *, state="accepted", plan="p1", evidence=None):
    db.execute(
        "INSERT INTO plan_nodes VALUES (?,?,?,?,NULL,?,?,1)",
        (node_id, plan, title, "Done when it is shipped.", state, "2026-09-21T12:00:00"),
    )
    if evidence is not None:
        db.execute(
            "INSERT INTO plan_node_events VALUES (?, 'review_evidence_submitted', ?, ?)",
            (node_id, json.dumps({
                "evidence": evidence, "requirements_revision": 1, "artifact_ids": [],
            }), "2026-09-21T12:00:00"),
        )
    db.commit()


@pytest.fixture
def logged():
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="INFO")
    yield lines
    logger.remove(sink)


def _related(db, title, *, caller="t2", criteria="", trigger="start"):
    from memory.node_start_recall import NodeText, related

    return related(
        db, NodeText(title, criteria), caller_thread_id=caller, trigger=trigger,
    )


def test_terms_put_the_title_first_and_drop_boilerplate():
    from memory.node_start_recall import MAX_TERMS, NodeText, terms

    chosen, title = terms(NodeText(
        "Fix the missed idle checkpoints",
        "Tests pass and the idle timer survives passive input; see #2283.",
    ))

    assert chosen[:3] == ["missed", "idle", "checkpoints"]
    assert title == {"missed", "idle", "checkpoints"}
    assert "fix" not in chosen and "tests" not in chosen and "the" not in chosen
    assert "2283" in chosen and len(chosen) <= MAX_TERMS


def test_an_owned_entry_from_another_thread_is_one_line_with_its_pointer(db, logged):
    cid = _install_checkpoint(
        db, thread_id="t1", narrative="Routine.", continuity=[{"statement": IDLE_ROOT_CAUSE}],
    )

    lines = _related(db, "Identify and fix missed idle checkpoints")

    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert "Root cause of missed idle checkpoints" in lines[0]
    assert f'get_checkpoints(checkpoint_id="{cid}") entry 1' in lines[0]
    assert any("reason=delivered trigger=start hits=1" in line for line in logged)


def test_older_epochs_are_reached_and_labelled(db):
    db.execute("UPDATE threads SET history_epoch=2 WHERE id='t1'")
    _install_checkpoint(
        db, thread_id="t1", narrative="Routine.", continuity=[{"statement": IDLE_ROOT_CAUSE}],
        history_epoch=1,
    )

    lines = _related(db, "missed idle checkpoints")

    assert len(lines) == 1 and "epoch 1" in lines[0]


def test_one_shared_term_is_below_the_floor(db, logged):
    _install_checkpoint(
        db, thread_id="t1", narrative="Routine.",
        continuity=[{"statement": "Checkpoints fold at 262k tokens on Claude Code seats."}],
    )

    assert _related(db, "Missed idle timers on quiet threads checkpoints") == []
    assert any("reason=below_floor" in line for line in logged)


def test_a_title_with_no_distinctive_terms_searches_nothing(db, logged):
    assert _related(db, "Fix the tests") == []
    assert any("reason=no_terms" in line for line in logged)


def test_another_users_entry_is_never_shown(db):
    _install_checkpoint(
        db, thread_id="t-foreign", narrative="Routine.", continuity=[{"statement": IDLE_ROOT_CAUSE}],
    )

    assert _related(db, "missed idle checkpoints") == []


def test_the_callers_own_live_entry_is_not_repeated_back(db):
    _install_checkpoint(
        db, thread_id="t2", narrative="Routine.", continuity=[{"statement": IDLE_ROOT_CAUSE}],
    )

    assert _related(db, "missed idle checkpoints") == []


def test_at_most_three_hits(db):
    for index in range(5):
        _install_checkpoint(
            db, thread_id="t1", narrative=f"Routine {index}.",
            continuity=[{"entry_id": index + 1, "field": "decisions",
                         "body": {"statement": f"{IDLE_ROOT_CAUSE} ({index})"},
                         "revision": 1}],
            checkpoint_id=f"cp-{index}",
        )

    assert len(_related(db, "missed idle checkpoints")) == 3


def test_a_settled_node_is_found_with_its_evidence(db):
    _node(db, "n-old", "Identify and fix missed idle checkpoints",
          evidence="Merged #2290; timer survives passive input.")
    _node(db, "n-open", "Missed idle checkpoints follow-up", state="active")
    _node(db, "n-foreign", "Missed idle checkpoints elsewhere", plan="pf")

    lines = _related(db, "Missed idle checkpoints on quiet threads")

    assert len(lines) == 1
    assert lines[0].startswith("node accepted · thread t1 · 2026-09-21")
    assert "Merged #2290" in lines[0] and lines[0].endswith(ELSEWHERE)


def test_another_threads_pointer_names_the_read_not_the_node(db):
    """Another thread's node is not readable: the line says so (#2520)."""
    _node(db, "n-old", "Identify and fix missed idle checkpoints")

    line = _related(db, "Missed idle checkpoints on quiet threads")[0]

    assert line.endswith(ELSEWHERE)
    assert "get_thread_history" in line and "not this node" in line
    assert "n-old" not in line


def test_a_node_in_the_callers_own_plan_points_at_plan_read(db):
    _node(db, "n-old", "Identify and fix missed idle checkpoints")

    lines = _related(db, "Missed idle checkpoints on quiet threads", caller="t1")

    assert lines[0].endswith('→ plan_read(node_id="n-old")')


def test_a_purged_threads_nodes_are_not_offered(db):
    _node(db, "n-old", "Identify and fix missed idle checkpoints")
    db.execute("UPDATE threads SET purged_at='2026-09-22T00:00:00' WHERE id='t1'")

    assert _related(db, "Missed idle checkpoints on quiet threads") == []


def test_the_node_being_started_is_not_its_own_hit(db):
    from memory.node_start_recall import NodeText, related

    _node(db, "n-self", "Missed idle checkpoints")

    text = NodeText("Missed idle checkpoints", node_id="n-self")
    assert related(db, text, caller_thread_id="t2", trigger="start") == []


def test_a_lookup_error_is_logged_and_never_raised(db, logged):
    db.execute("DROP TABLE plan_nodes")

    assert _related(db, "missed idle checkpoints") == []
    assert any(
        "reason=error" in line and "OperationalError: no such table" in line
        for line in logged
    )


def test_attach_adds_the_key_only_for_starting_operations_with_hits(db):
    from memory.node_start_recall import RESULT_KEY, attach_to_result

    _node(db, "n-old", "Identify and fix missed idle checkpoints")
    node = {"id": "n-new", "title": "Missed idle checkpoints again",
            "acceptance_criteria": "Timer survives.", "description": None}

    started = {"node": dict(node)}
    attach_to_result(db, "start", started, caller_thread_id="t2")
    edited = {"node": dict(node)}
    attach_to_result(db, "edit", edited, caller_thread_id="t2")
    quiet = {"node": {**node, "title": "Rename the release notes heading"}}
    attach_to_result(db, "add", quiet, caller_thread_id="t2")

    assert started[RESULT_KEY][0].endswith(ELSEWHERE)
    assert RESULT_KEY not in edited and RESULT_KEY not in quiet


def test_a_close_that_started_the_next_task_carries_it(db):
    from memory.node_start_recall import RESULT_KEY, attach_to_result

    _node(db, "n-old", "Identify and fix missed idle checkpoints")
    _node(db, "n-next", "Missed idle checkpoints again", state="active")
    result = {"next": {"selected": {"id": "n-next", "how": "started"}}}
    running = {"next": {"selected": {"id": "n-next", "how": "running"}}}

    attach_to_result(db, "request_review", result, caller_thread_id="t2")
    attach_to_result(db, "request_review", running, caller_thread_id="t2")

    assert result[RESULT_KEY][0].endswith(ELSEWHERE)
    assert RESULT_KEY not in running


def test_a_raised_decision_is_looked_up_by_its_title_and_context(db):
    from memory.node_start_recall import RESULT_KEY, attach_to_result

    _node(db, "n-old", "Identify and fix missed idle checkpoints")
    result = {"decision": {"title": "Missed idle checkpoints: fix now?",
                           "context": "Quiet threads never fold."}}

    attach_to_result(db, "raise", result, caller_thread_id="t2")

    assert result[RESULT_KEY][0].endswith(ELSEWHERE)


def test_opening_a_delivered_pointer_is_logged_once(db, logged):
    from memory.node_start_opened import note_opened

    _node(db, "n-old", "Identify and fix missed idle checkpoints")
    _related(db, "Missed idle checkpoints again")

    note_opened("t2", "t-unrelated")
    note_opened("t3", "t1")
    note_opened("t2", "t1")
    note_opened("t2", "t1")

    assert sum("reason=opened" in line for line in logged) == 1
