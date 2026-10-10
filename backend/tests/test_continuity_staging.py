"""Live continuity and disposable checkpoint edits share one explicit seam."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest


def _connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _db(path: str = ":memory:") -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = _connect(path)
    db.executescript(SCHEMA_SQL)
    return db


def _seed_thread(db: sqlite3.Connection, thread_id: str = "thread-1") -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES (?, 'user-1', 'native/Fortissimo', 0, 2, 't0', 't0')",
        (thread_id,),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES (?,?,'user','request','t0',1)",
        (f"{thread_id}-m1", thread_id),
    )
    db.commit()


def _statement(text: str) -> dict:
    return {"statement": text, "source_refs": []}


def _request(thread_id: str, checkpoint_id: str):
    from core.checkpoint_types import CheckpointRequest

    return CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=0,
        previous_generation_id=None,
        through_seq=1,
        through_message_id=f"{thread_id}-m1",
        trigger="pressure",
        mode="warm",
        source_harness="native",
        source_model="native/Fortissimo",
        writer_harness="native",
        writer_model="native/Fortissimo",
        originating_turn_id="turn-1",
        first_included_seq=1,
        started_at="t1",
    )


def _running(db: sqlite3.Connection, checkpoint_id: str = "cp-1"):
    from infra.checkpoints import create_running

    return create_running(_request("thread-1", checkpoint_id), db=db)


def _capture_install(db: sqlite3.Connection, checkpoint_id: str = "cp-1"):
    from infra.checkpoints import capture, install

    capture(
        checkpoint_id,
        raw_result="usable body",
        usage_json=None,
        captured_at="t2",
        db=db,
    )
    return install(checkpoint_id, installed_at="t3", db=db)


def _tool_db(path, monkeypatch):
    from tools import continuity

    monkeypatch.setattr(continuity, "get_db", lambda: _connect(str(path)))
    return continuity


def test_live_add_replace_drop_and_error_isolation(tmp_path, monkeypatch) -> None:
    path = tmp_path / "live.db"
    db = _db(str(path))
    _seed_thread(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    bad = continuity._run(
        "thread-1", "add", {"field": "facts", "body": {"bad": True}}
    )
    added = continuity._run(
        "thread-1", "add", {"field": "facts", "body": _statement("before")}
    )
    replaced = continuity._run(
        "thread-1", "replace",
        {"entry_id": 1, "body": _statement("after")},
    )
    dropped = continuity._run(
        "thread-1", "drop", {"entry_id": 1, "reason": "stale"}
    )

    assert bad.startswith("Error: invalid facts entry")
    assert (added, replaced, dropped) == (
        "Recorded as entry 1 in facts.",
        "Entry 1 replaced.",
        "Entry 1 retired.",
    )


def test_live_replace_accepts_a_matching_field_and_rejects_a_mismatch(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "live-field.db"
    db = _db(str(path))
    _seed_thread(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)
    continuity._run("thread-1", "add", {"field": "facts", "body": _statement("before")})

    matched = continuity._run(
        "thread-1", "replace",
        {"entry_id": 1, "field": "facts", "body": _statement("after")},
    )
    mismatched = continuity._run(
        "thread-1", "drop",
        {"entry_id": 1, "field": "decisions", "reason": "wrong field"},
    )

    assert matched == "Entry 1 replaced."
    assert mismatched == (
        "Error: field decisions does not match entry 1's field facts"
    )


def test_live_inspect_field_filters_the_listing(tmp_path, monkeypatch) -> None:
    path = tmp_path / "live-inspect-field.db"
    db = _db(str(path))
    _seed_thread(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)
    continuity._run("thread-1", "add", {"field": "facts", "body": _statement("a fact")})
    continuity._run(
        "thread-1", "add",
        {"field": "active_constraints", "body": _statement("a constraint")},
    )

    result = json.loads(continuity._run("thread-1", "inspect", {"field": "facts"}))

    assert set(result["entries"]) == {"facts"}
    assert result["entries"]["facts"][0]["statement"] == "a fact"


def test_checkpoint_inspect_is_frozen_and_staged_add_reserves_real_id(
    tmp_path, monkeypatch
) -> None:
    from infra.continuity_store import active_entries, add_entry, replace_entry

    path = tmp_path / "staged.db"
    db = _db(str(path))
    _seed_thread(db)
    first = add_entry(
        db,
        thread_id="thread-1",
        history_epoch=0,
        field="facts",
        body=_statement("frozen"),
    )
    _running(db)
    replace_entry(
        db,
        thread_id="thread-1",
        history_epoch=0,
        entry_id=first,
        body=_statement("live changed"),
    )
    db.commit()
    db.close()
    continuity = _tool_db(path, monkeypatch)

    frozen = json.loads(continuity._run("thread-1", "inspect", {}, "cp-1"))
    added = continuity._run(
        "thread-1",
        "add",
        {"field": "facts", "body": _statement("staged")},
        "cp-1",
    )
    replaced = continuity._run(
        "thread-1",
        "replace",
        {"entry_id": 2, "body": _statement("staged revised")},
        "cp-1",
    )

    assert frozen["entries"]["facts"][0]["statement"] == "frozen"
    assert added == "Staged as entry 2 in facts."
    assert replaced == "Staged entry 2 replacement."
    db = _connect(str(path))
    assert active_entries(db, "thread-1", 0)["facts"] == [
        {"entry_id": 1, **_statement("live changed")}
    ]
    live_id = add_entry(
        db,
        thread_id="thread-1",
        history_epoch=0,
        field="facts",
        body=_statement("live add"),
    )
    assert live_id == 3


def test_targeted_inspect_reads_frozen_plus_staged_state(tmp_path, monkeypatch) -> None:
    from infra.continuity_store import add_entry, replace_entry

    path = tmp_path / "targeted-inspect.db"
    db = _db(str(path))
    _seed_thread(db)
    first = add_entry(
        db, thread_id="thread-1", history_epoch=0, field="facts",
        body=_statement("frozen"),
    )
    _running(db)
    replace_entry(
        db, thread_id="thread-1", history_epoch=0, entry_id=first,
        body=_statement("live changed"),
    )
    db.commit()
    db.close()
    continuity = _tool_db(path, monkeypatch)

    frozen = json.loads(continuity._run(
        "thread-1", "inspect", {"entry_id": first}, "cp-1",
    ))
    continuity._run(
        "thread-1", "replace",
        {"entry_id": first, "body": _statement("staged")}, "cp-1",
    )
    staged = json.loads(continuity._run(
        "thread-1", "inspect", {"entry_id": first}, "cp-1",
    ))
    continuity._run(
        "thread-1", "drop", {"entry_id": first, "reason": "gone"}, "cp-1",
    )
    missing = continuity._run(
        "thread-1", "inspect", {"entry_id": first}, "cp-1",
    )
    live = json.loads(continuity._run(
        "thread-1", "inspect", {"entry_id": first}, None,
    ))

    assert frozen["entry"]["statement"] == "frozen"
    assert staged["entry"]["statement"] == "staged"
    assert missing == f"Error: continuity entry {first} does not exist"
    assert live["entry"]["statement"] == "live changed"


def test_frozen_continuity_index_contains_ids_not_bodies() -> None:
    from infra.continuity_staging import frozen_continuity_index
    from infra.continuity_store import add_entry

    db = _db()
    _seed_thread(db)
    entry_id = add_entry(
        db, thread_id="thread-1", history_epoch=0, field="facts",
        body=_statement("SECRET BODY"),
    )
    _running(db)

    index = frozen_continuity_index("cp-1", "thread-1", db=db)

    assert json.loads(index) == {"facts": [entry_id]}
    assert "SECRET BODY" not in index


def test_invalid_staged_edit_rolls_back_without_consuming_an_id(
    tmp_path, monkeypatch
) -> None:
    from infra.continuity_store import add_entry

    path = tmp_path / "rollback.db"
    db = _db(str(path))
    _seed_thread(db)
    _running(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    result = continuity._run(
        "thread-1", "add", {"field": "facts", "body": {"bad": True}}, "cp-1"
    )

    db = _connect(str(path))
    assert result.startswith("Error: invalid facts entry")
    assert db.execute("SELECT COUNT(*) FROM checkpoint_staged_edits").fetchone()[0] == 0
    assert add_entry(
        db,
        thread_id="thread-1",
        history_epoch=0,
        field="facts",
        body=_statement("first"),
    ) == 1


def test_install_merges_unrelated_edits_and_live_wins_same_entry_conflict(
    tmp_path, monkeypatch
) -> None:
    from infra.continuity_store import active_entries, add_entry, replace_entry

    path = tmp_path / "merge.db"
    db = _db(str(path))
    _seed_thread(db)
    first = add_entry(
        db, thread_id="thread-1", history_epoch=0,
        field="facts", body=_statement("first"),
    )
    second = add_entry(
        db, thread_id="thread-1", history_epoch=0,
        field="facts", body=_statement("second"),
    )
    _running(db)
    db.commit()
    db.close()
    continuity = _tool_db(path, monkeypatch)
    continuity._run(
        "thread-1", "replace",
        {"entry_id": first, "body": _statement("fold first")}, "cp-1",
    )
    continuity._run(
        "thread-1", "replace",
        {"entry_id": second, "body": _statement("fold second")}, "cp-1",
    )
    db = _connect(str(path))
    replace_entry(
        db, thread_id="thread-1", history_epoch=0, entry_id=first,
        body=_statement("live first"),
    )
    db.commit()

    installed = _capture_install(db)

    assert installed.status == "installed"
    entries = active_entries(db, "thread-1", 0)["facts"]
    assert [entry["statement"] for entry in entries] == ["live first", "fold second"]
    outcomes = db.execute(
        "SELECT entry_id,outcome FROM checkpoint_staged_edits ORDER BY edit_order"
    ).fetchall()
    assert [tuple(row) for row in outcomes] == [(first, "conflict"), (second, "applied")]
    snapshot = json.loads(installed.continuity_snapshot_json)
    assert {entry["body"]["statement"] for entry in snapshot} == {
        "live first", "fold second"
    }


def test_staged_add_can_be_replaced_then_dropped_before_install(
    tmp_path, monkeypatch
) -> None:
    from infra.continuity_store import active_entries

    path = tmp_path / "staged-sequence.db"
    db = _db(str(path))
    _seed_thread(db)
    _running(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)
    continuity._run(
        "thread-1", "add",
        {"field": "facts", "body": _statement("new")}, "cp-1",
    )
    continuity._run(
        "thread-1", "replace",
        {"entry_id": 1, "body": _statement("newer")}, "cp-1",
    )
    continuity._run(
        "thread-1", "drop", {"entry_id": 1, "reason": "unused"}, "cp-1",
    )
    db = _connect(str(path))

    _capture_install(db)

    assert active_entries(db, "thread-1", 0)["facts"] == []
    assert [row[0] for row in db.execute(
        "SELECT outcome FROM checkpoint_staged_edits ORDER BY edit_order"
    )] == ["applied", "applied", "applied"]


@pytest.mark.asyncio
async def test_checkpoint_harness_tools_skip_turn_lineage_and_keep_scope() -> None:
    from infra.continuity_staging import current_checkpoint_id
    from subagents.harness_tool_bridge import execute_tool

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, name, arguments):
            assert (name, arguments, current_checkpoint_id()) == (
                "ordinary_tool", {"value": 1}, "cp-1"
            )
            return "ordinary result"

    result = await execute_tool(
        Registry(),
        thread_id="thread-1",
        checkpoint_id="cp-1",
        name="ordinary_tool",
        arguments={"value": 1},
    )

    assert type(result) is str and result == "ordinary result"


def test_checkpoint_metadata_is_reserved_but_tool_surfaces_do_not_change(tmp_path) -> None:
    from infra.continuity_staging import checkpoint_execution_scope
    from subagents.harness_tool_bridge import write_cc_mcp_config
    from tools.continuity import ContinuityEditTool
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ContinuityEditTool())
    before = registry.get_definitions()
    first = json.loads(open(write_cc_mcp_config(
        thread_id="thread-1", tool_token="token"
    ), encoding="utf-8").read())
    with checkpoint_execution_scope("cp-1"):
        assert registry.get_definitions() == before
        assert registry._strip_reserved_context(
            {"operation": "inspect", "_checkpoint_id": "spoof"}
        ) == {"operation": "inspect"}
        second = json.loads(open(write_cc_mcp_config(
            thread_id="thread-1", tool_token="token"
        ), encoding="utf-8").read())

    assert first == second


def test_continuity_validation_names_the_parameter_rejected_for_an_operation() -> None:
    """`field` is now legal on drop (#1653); `body` still is not."""
    from tools.continuity import ContinuityEditTool

    errors = ContinuityEditTool().validate_params({
        "operation": "drop",
        "body": {"statement": "s", "source_refs": []},
        "entry_id": 4,
        "reason": "superseded",
    })

    assert errors == [
        "unexpected body (valid keys: entry_id, field, operation, reason, source_ref)"
    ]


def test_live_and_checkpoint_calls_route_concurrently(tmp_path, monkeypatch) -> None:
    path = tmp_path / "concurrent.db"
    db = _db(str(path))
    _seed_thread(db)
    _running(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        live = pool.submit(
            continuity._run,
            "thread-1", "add", {"field": "facts", "body": _statement("live")},
        )
        folded = pool.submit(
            continuity._run,
            "thread-1", "add", {"field": "facts", "body": _statement("fold")},
            "cp-1",
        )
        assert live.result().startswith("Recorded as entry")
        assert folded.result().startswith("Staged as entry")

    db = _connect(str(path))
    canonical = db.execute(
        "SELECT body FROM continuity_entries WHERE state='active'"
    ).fetchall()
    staged = db.execute(
        "SELECT body FROM checkpoint_staged_edits WHERE outcome='pending'"
    ).fetchall()
    assert [json.loads(row[0])["statement"] for row in canonical] == ["live"]
    assert [json.loads(row[0])["statement"] for row in staged] == ["fold"]


def test_checkpoint_transport_id_must_name_running_row_for_exact_thread() -> None:
    from infra.continuity_staging import running_checkpoint_for_thread
    from infra.checkpoints import cancel

    db = _db()
    _seed_thread(db)
    _running(db)

    assert running_checkpoint_for_thread(db, "cp-1", "thread-1")
    assert not running_checkpoint_for_thread(db, "cp-1", "wrong-thread")
    cancel("cp-1", ended_at="t2", db=db)
    assert not running_checkpoint_for_thread(db, "cp-1", "thread-1")
