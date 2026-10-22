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


def test_set_aside_until_is_the_earliest_instant_as_written():
    nodes = [
        _node(_wait("2026-10-22T09:30:00-07:00", "Later", scheduled_reason="set aside")),
        _node(_wait("2026-10-22T16:00:00Z", "Sooner", scheduled_reason="set aside")),
    ]

    assert wait_summary.set_aside_until(nodes) == "2026-10-22T16:00:00Z"
    assert wait_summary.set_aside_until([]) is None
