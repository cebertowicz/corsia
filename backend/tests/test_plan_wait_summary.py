"""The wait summary picks the earliest wait by instant, not by how its time is written."""
from __future__ import annotations

from plans import wait_summary


def _node(*causes: dict) -> dict:
    return {"title": "Task", "execution": {"condition": "waiting", "causes": list(causes)}}


def _wait(ends_at: str, reason: str, **extra) -> dict:
    return {"kind": "waiting", "ends_at": ends_at, "blocked_reason": reason, **extra}


def test_the_earliest_wait_is_chosen_across_offset_forms():
    # 16:00Z is earlier than 09:30 at -07:00 (16:30Z), though it sorts later as a string.
    nodes = [_node(_wait("2026-10-22T09:30:00-07:00", "Later")), _node(_wait("2026-10-22T16:00:00Z", "Sooner"))]

    summary = wait_summary.from_nodes(nodes)

    assert (summary["label"], summary["until"], summary["more"]) == ("Sooner", "2026-10-22T16:00:00Z", 1)


def test_a_wait_nothing_names_reads_as_the_check_it_ends_in():
    # Surfaces show "Waiting for {label}", so the waiting task's own title would read as what it waits for.
    unnamed = {"kind": "waiting", "ends_at": "2026-10-22T16:00:00Z"}

    assert wait_summary.from_nodes([_node(unnamed)])["label"] == "a scheduled check"
    assert wait_summary.from_nodes([_node({**unnamed, "artifact_id": "a1"})])["label"] == "the linked item"


def test_a_child_plans_wait_carries_its_tasks_title(monkeypatch):
    from plans import node_waits

    cause = {"kind": "waiting", "ends_at": "2026-10-22T16:00:00Z", "child_thread_id": "child"}
    monkeypatch.setattr(node_waits, "armed_in_plans", lambda _db, _ids: {"plan": [("watch-row", "Ship the export")]})
    monkeypatch.setattr(node_waits, "named_watch_cause", lambda row: cause if row == "watch-row" else {})

    assert wait_summary.for_plans(None, ["plan"])["plan"]["label"] == "an agent working on Ship the export"


def test_set_aside_until_is_the_earliest_instant_as_written():
    nodes = [
        _node(_wait("2026-10-22T09:30:00-07:00", "Later", scheduled_reason="set aside")),
        _node(_wait("2026-10-22T16:00:00Z", "Sooner", scheduled_reason="set aside")),
    ]

    assert wait_summary.set_aside_until(nodes) == "2026-10-22T16:00:00Z"
    assert wait_summary.set_aside_until([]) is None
