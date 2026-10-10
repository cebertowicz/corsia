"""The frozen browser fixture uses the real current schema and canonical projections."""
from pathlib import Path
import runpy

from plans import operator_chat_state
from tests.plan_close_next_helpers import HUMAN
from tests.test_thread_updates import db  # noqa: F401 - fixture


def test_frozen_outcome_fixture_seeds_schema_and_expected_query_contract(db):
    fixture = Path(__file__).resolve().parents[2] / "frontend/e2e/support/focus-outcomes.py"
    db.execute("UPDATE threads SET spawned_by=NULL WHERE id='worker'")
    seeded = runpy.run_path(str(fixture))["seed"](db, "owner", "other", "worker")
    assert seeded["live_close"] and seeded["live_revision"] == 1
    assert seeded["times"] == [
        "2026-09-29T09:00:01.123456", "2026-09-29T09:00:01.654321",
        "2026-09-29T09:02:01.000000", "2026-09-29T09:02:02.000000", "2026-09-29T11:00:01.000000",
        "2026-09-29T11:01:01.000000", "2026-09-29T11:02:01.000000", "2026-09-29T11:03:01.000000",
        "2026-09-29T11:02:30.000000",
    ]
    states = operator_chat_state.read_thread_states(["owner", "other", "worker"], actor=HUMAN, db=db)["states"]
    assert states["owner"]["has_update"] is True
    assert states["owner"]["latest_update"]["summary"] == "Completed: Latest completed snapshot"
    assert states["other"]["has_update"] is False
    assert states["other"]["latest_update"]["summary"] == "Closed: Passive sidebar snapshot"
    assert [item["title"] for item in states["worker"]["attention_items"]] == ["Failure assigned to user"]


def test_repeated_independent_browser_seeds_share_schema_allocated_turn_order(db):
    fixture = Path(__file__).resolve().parents[2] / "frontend/e2e/support/focus-outcomes.py"
    seed = runpy.run_path(str(fixture))["seed"]
    groups = [("main-a", "passive-a", "failed-a"), ("main-b", "passive-b", "failed-b")]
    for group in groups:
        for thread in group:
            db.execute(
                "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?, 'user', 'Proof', 't', 't')",
                (thread,),
            )
    first = seed(db, *groups[0])
    second = seed(db, *groups[1])
    assert first["times"] == second["times"]
    turns = db.execute("SELECT turn_order FROM active_turns ORDER BY turn_order").fetchall()
    assert len(turns) == 6 and len({row[0] for row in turns}) == 6
    for main, passive, failed in groups:
        states = operator_chat_state.read_thread_states([main, passive, failed], actor=HUMAN, db=db)["states"]
        assert states[main]["latest_update"]["summary"] == "Completed: Latest completed snapshot"
        assert states[passive]["has_update"] is False
        assert [item["title"] for item in states[failed]["attention_items"]] == ["Failure assigned to user"]
        pause = db.execute("SELECT status,error,started_at,ended_at FROM active_turns WHERE id=?", (f"{main}-paused",)).fetchone()
        assert tuple(pause) == ("cancelled", "plan_limit_overage_off", "2026-09-29T09:00:00", "2026-09-29T11:10:00")
