"""Actual canonical writer plus guidance receipts, without nearest-time matching."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.lesson_delivery import TrialDelivery, _insert
from core.lesson_report_boundaries import identity_for_raw
from core.lesson_report_receipts import read_offers
from core.lesson_report_reads import read_bodies
from core.tool_result_delivery import persist_tool_execution_result
from infra.queries import insert_message
from tests.test_lesson_report_inventory import source
from tests.test_lesson_report_receipts import db

PATH = "/authorized/skills/deciding/SKILL.md"


def read_call(db, *, call_id="call", path=PATH, thread="owner", error=False, synthetic=False, dispatched=False):
    args = {"path": path}
    name = "read_file"
    if dispatched:
        name, args = "tool_call", {"name": "read_file", "arguments": args}
    step = f"step-{call_id}"
    insert_message(thread, "assistant", "", {
        "turn_id": "turn", "tool_calls": [{"id": call_id, "name": name, "arguments": args}],
    }, msg_id=step, db=db)
    return persist_tool_execution_result(
        thread_id=thread, turn_id="turn", invocation_id=call_id, assistant_step_id=step,
        provider_tool_call_id=call_id, name=name, arguments=args, content="read body", is_error=error,
        extra_metadata={"synthetic_tool_result": True} if synthetic else None, db=db,
    )


def load(db, *, identity="load", path=PATH, thread="owner", partial=False, digest=None):
    current = source()
    db.execute(
        "INSERT INTO guidance_loads(id,thread_id,turn_id,path,raw_sha256,git_blob_oid,"
        "delivered_sha256,byte_count,partial,created_at) VALUES(?,?,'turn',?,?,?,? ,100,?,'2026-09-21')",
        (identity, thread, path, digest or current["raw_sha256"], "a" * 40, "b" * 64, int(partial)),
    )
    return current


def bodies(db, facts):
    return read_bodies(db, user_id="user", thread_ids=("owner",), facts=facts)


def test_lifetime_candidate_stream_never_materializes_tool_metadata(db):
    from collections import Counter
    from core.lesson_report_reads import _candidate_groups

    read_call(db)

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            pytest.fail("lifetime candidate stream called fetchall")

    class StreamingDB:
        def execute(self, *args):
            return StreamingCursor(db.execute(*args))

    groups = _candidate_groups(StreamingDB(), "user", ("owner",), Counter())
    assert list(groups) == [("owner", "turn", PATH)]


@pytest.mark.parametrize("partial,dispatched", [(False, False), (True, False), (False, True)])
def test_one_exact_full_or_partial_read_keeps_behavior_and_canonical_result_reference(db, partial, dispatched):
    result = read_call(db, dispatched=dispatched)
    current = load(db, partial=partial)
    projection = bodies(db, [current])
    [read] = projection["reads"]
    assert (read["id"], read["version"]) == (current["id"], current["version"])
    assert read["partial"] is partial and read["offered"] is False
    assert read["result_reference"]["message_id"] == result.message_id
    assert read["task_id"] is None and read["provider_consumed"] is None
    assert read["body_tokens"] is None and projection["unavailable_reads"] == {}


@pytest.mark.parametrize("repeated", ["calls", "loads"])
def test_repeated_reads_are_ambiguous_even_when_a_date_would_pick_one(db, repeated):
    read_call(db)
    current = load(db)
    if repeated == "calls":
        read_call(db, call_id="again")
    else:
        load(db, identity="again")
    projection = bodies(db, [current])
    assert projection["reads"] == []
    assert projection["unavailable_reads"] == {"repeated or missing call/load linkage": 2 if repeated == "calls" else 1}


@pytest.mark.parametrize("fault", ["relative", "failed", "synthetic", "bytes", "step"])
def test_unproven_or_failed_results_never_become_body_consumption(db, fault):
    result = read_call(db, path="skills/deciding/SKILL.md" if fault == "relative" else PATH,
                       error=fault == "failed", synthetic=fault == "synthetic")
    current = load(db)
    if fault == "bytes":
        db.execute("UPDATE messages SET content='altered' WHERE id=?", (result.message_id,))
    if fault == "step":
        db.execute("DELETE FROM messages WHERE id='step-call'")
    projection = bodies(db, [current])
    assert projection["reads"] == []
    assert sum(projection["unavailable_reads"].values()) == 1


def test_old_native_and_cross_turn_lineage_stay_named_unknowns(db):
    result = read_call(db)
    current = load(db)
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (result.message_id,)).fetchone()
    native = json.loads(row["metadata"])
    native.pop("tool_invocation_id")
    db.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(native), result.message_id))
    native_projection = bodies(db, [current])
    assert native_projection["unavailable_reads"] == {"canonical invocation lineage unavailable": 1}
    assert "native results before the native-writer fix lack invocation lineage" in native_projection["coverage"]
    assert "provider-call-id reuse across Turns" in native_projection["coverage"]

    second_path = PATH + ".second"
    result = read_call(db, call_id="second", path=second_path)
    load(db, identity="second", path=second_path)
    step = db.execute("SELECT metadata FROM messages WHERE id='step-second'").fetchone()
    metadata = json.loads(step["metadata"])
    metadata["turn_id"] = "later-turn"
    db.execute("UPDATE messages SET metadata=? WHERE id='step-second'", (json.dumps(metadata),))
    projection = bodies(db, [current])
    assert projection["unavailable_reads"]["Assistant Step Turn lineage unavailable"] == 1


def test_an_unavailable_recorded_object_keeps_raw_evidence_without_current_version(db):
    result = read_call(db)
    current = load(db, digest="c" * 64)
    projection = bodies(db, [current])
    [read] = projection["reads"]
    assert read["id"] is None and read["version"] is None
    assert read["identity_reason"] == "workspace guidance history unavailable"
    assert read["raw_sha256"] == "c" * 64 and read["path"] == PATH
    assert read["result_reference"]["message_id"] == result.message_id
    assert projection["unavailable_reads"] == {}


def test_foreign_and_bare_orphan_paths_do_not_disclose_reads(db):
    read_call(db, thread="foreign")
    current = load(db, thread="foreign")
    projection = read_bodies(db, user_id="user", thread_ids=("foreign", "orphan"), facts=[current])
    assert projection["reads"] == [] and projection["unavailable_reads"] == {}


def test_delivery_receipt_keeps_orphan_behavioral_raw_identity_without_prompt_data(db):
    trial = TrialDelivery(
        "orphan-lesson", "a" * 64, Path(PATH), b"private bytes",
        "skills/deciding/SKILL.md", "Trial guidance.",
    )
    _insert(db, thread_id="owner", node_id="deleted-owner-task", trial=trial, variant="with",
            checkpoint_generation_id=None)

    [fact] = read_offers(
        db, user_id="user", thread_ids=("owner", "foreign", "orphan"),
        from_ts="2000-01-01", to_ts="2100-01-01",
    )["offers"]

    assert fact["offered"] is True and fact["path"] == PATH
    assert "private bytes" not in json.dumps(fact)
    assert identity_for_raw([fact], digest=fact["raw_sha256"], path=PATH) == {
        "kind": "lesson", "id": "orphan-lesson", "version": "a" * 64,
    }
    assert identity_for_raw([fact, fact | {"version": "other"}], digest=fact["raw_sha256"], path=PATH) is None
