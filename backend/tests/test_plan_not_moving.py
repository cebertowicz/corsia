"""A plan whose open work cannot move keeps one set of reasons (stuck plans R9).

One predicate (``plans.not_moving``) decides it. Human surfaces retain the
four-state vocabulary while chat-list liveness and the agent digest consume the
same underlying facts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from plans import (
    not_moving, operator_chat_state, operator_node_state, operator_nodes, operator_plan_display,
    operator_projection, service, status,
    status_context,
)
from tests.plan_close_next_helpers import HUMAN, pause, plan_update, revision, task
from tests.test_plan_chat_list_state import _plan, _states, _thread
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SOON = "2026-10-01T17:00:00+00:00"
LATER = "2026-10-03T09:00:00Z"


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """The fixed dates below stay in the future however long this file lives."""
    monkeypatch.setattr(not_moving, "_utcnow", lambda: NOW)


_LABELS = {
    "waiting": "waiting", "blocked": "waiting", "paused": "waiting",
    "review": "working", "running": "working", "ready": "working",
}


def _leaf(node_id: str, condition: str, *causes: dict, review: str | None = None) -> dict:
    return {
        "id": node_id, "title": f"Task {node_id}", "parent_id": None,
        "state": "review_requested" if condition == "review" else "active",
        "execution": {"condition": condition, "causes": list(causes)},
        "review": {"kind": review} if review else None,
        "requires_user_action": review in {"human_decision", "human_review"},
        "display": {
            "label_key": (
                "needs_you" if review in {"human_decision", "human_review"}
                else _LABELS[condition]
            ),
            "tone_key": (
                "human-review" if review in {"human_decision", "human_review"}
                else "waiting"
            ),
        },
    }


def _wait(ends_at: str | None, *, scheduled_reason: str | None = None) -> dict:
    cause = {"kind": "waiting", "reason_key": "deadline", "owner_kind": "thread", "ends_at": ends_at}
    if scheduled_reason is not None:
        cause["scheduled_reason"] = scheduled_reason
    return cause


_USER_HOLD = {"placed": True, "kind": "blocked", "owner_kind": "user", "owner_id": None}
_USER_PAUSE = {"placed": True, "kind": "paused", "owner_kind": "user", "owner_id": None}
_AGENT_PAUSE = {"placed": True, "kind": "paused", "owner_kind": "thread", "owner_id": "agent"}


def _header(
    nodes: list[dict], plan_state: str = "active", users: frozenset[str] = frozenset(),
) -> tuple[str, dict]:
    counts = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)
    for node in nodes:
        counts[operator_node_state.leaf_bucket(node)] += 1
    display = operator_plan_display.plan_display({"state": plan_state}, counts, nodes, users)
    still = "not_moving" in display
    return ("not_moving" if still else operator_node_state.leading(counts)), display


def _parts(
    need_you=0, paused_by_you=0, ends=None, overdue=0, next_overdue=None,
    set_aside=0, overdue_wait_entries=None,
):
    """The facts ``not_moving.read`` returns; a dated wait turns overdue after the grace."""
    if ends and next_overdue is None:
        next_overdue = (
            datetime.fromisoformat(ends.replace("Z", "+00:00")) + not_moving.OVERDUE_GRACE
        ).isoformat()
    facts = {
        "need_you": need_you, "next_wait_ends_at": ends, "next_overdue_at": next_overdue,
        "overdue_waits": overdue, "paused_by_you": paused_by_you, "set_aside": set_aside,
    }
    if overdue_wait_entries:
        facts["overdue_wait_entries"] = overdue_wait_entries
    return facts


_CASES = {
    "need_you_only": (
        [_leaf("r", "review", review="human_review"), _leaf("h", "blocked", _USER_HOLD)],
        "human-review", _parts(need_you=1),
    ),
    "deadline_only": (
        [_leaf("a", "waiting", _wait(LATER)), _leaf("b", "waiting", _wait(SOON)),
         _leaf("c", "waiting", _wait(None))],
        "waiting", _parts(ends=SOON),
    ),
    "paused_by_you_only": (
        [_leaf("p", "paused", _USER_PAUSE), _leaf("q", "paused", _AGENT_PAUSE)],
        "waiting", _parts(paused_by_you=1),
    ),
    "all_three": (
        [_leaf("p", "paused", _USER_PAUSE), _leaf("w", "waiting", _wait(SOON)),
         _leaf("r", "review", review="human_decision")],
        "human-review", _parts(need_you=1, ends=SOON, paused_by_you=1),
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_a_still_plan_leads_not_moving_and_names_its_parts(case):
    nodes, tone, parts = _CASES[case]
    lead, display = _header(nodes)
    assert lead == "not_moving"
    label = "needs_you" if tone == "human-review" else "waiting"
    assert display == {"label_key": label, "tone_key": tone, "not_moving": parts}


def test_moving_work_keeps_its_lead():
    lead, display = _header([
        _leaf("r", "review", review="human_review"), _leaf("run", "running"),
        _leaf("w", "waiting", _wait(SOON)),
    ])
    assert lead == "review_human"
    assert display["label_key"] == "working"


def test_a_ready_task_is_not_parts():
    lead, display = _header([_leaf("w", "waiting", _wait(SOON)), _leaf("go", "ready")])
    assert lead == "open"
    assert display["label_key"] == "working"


def test_a_ready_task_assigned_to_the_user_is_not_motion_for_the_agent():
    """R9: the user's ready task needs the user; the agent cannot move it."""
    lead, display = _header(
        [_leaf("u", "ready"), _leaf("w", "waiting", _wait(SOON))], users=frozenset({"u"}),
    )
    assert lead == "not_moving"
    assert display == {
        "label_key": "needs_you", "tone_key": "human-review",
        "not_moving": _parts(need_you=1, ends=SOON),
    }
    # Beside a ready task of the agent's own, the plan still reads Ready.
    lead, display = _header([_leaf("u", "ready"), _leaf("go", "ready")], users=frozenset({"u"}))
    assert (lead, display["label_key"]) == ("open", "working")


def test_a_finished_user_task_no_longer_needs_the_user(db):
    _thread(db, "settled-user")
    plan = _plan(db, "settled-user")
    user_task = task(db, plan.id, "Sign the contract", assignee="user")
    plan_update(db, plan.id, "mark_done", {
        "node_id": user_task.id, "expected_revision": revision(db, user_task.id),
    }, actor=HUMAN)

    projected = operator_projection.read_thread(
        "settled-user", actor=HUMAN, db=db,
    )["plans"][0]
    shown = next(node for node in projected["nodes"] if node["id"] == user_task.id)

    assert shown["requires_user_action"] is False
    assert shown["display"]["label_key"] == "finished"
    assert projected["display"]["label_key"] == "finished"


def test_a_user_task_behind_a_prerequisite_does_not_need_them_yet(db):
    _thread(db, "sequenced-user")
    plan = _plan(db, "sequenced-user")
    prerequisite = task(db, plan.id, "Prepare the contract")
    user_task = task(db, plan.id, "Sign the contract", assignee="user")
    service.add_dependency(user_task.id, prerequisite.id, db=db)
    db.commit()

    projected = operator_projection.read_thread(
        "sequenced-user", actor=HUMAN, db=db,
    )["plans"][0]
    shown = next(node for node in projected["nodes"] if node["id"] == user_task.id)

    assert shown["execution"]["condition"] == "blocked"
    assert shown["requires_user_action"] is False
    assert shown["display"]["label_key"] == "waiting"


def test_a_blocker_only_the_agent_can_clear_reads_waiting_without_a_fifth_state():
    """A failed prerequisite is the agent's to re-plan: not a still plan, and not the user's."""
    failed_prerequisite = {
        "kind": "blocked", "reason_key": "dependency_failed", "owner_kind": "system",
    }
    lead, display = _header([
        _leaf("f", "blocked", failed_prerequisite), _leaf("w", "waiting", _wait(SOON)),
    ])
    assert lead == "blocked"
    assert display == {"label_key": "waiting", "tone_key": "waiting"}


def test_a_plan_the_user_paused_counts_every_paused_leaf_as_theirs():
    nodes = [
        _leaf("r", "review", review="human_review"),
        _leaf("p", "paused", {"kind": "paused", "reason_key": "plan_paused", "owner_kind": "thread"}),
    ]
    _lead, display = _header(nodes, plan_state="paused")
    assert display["not_moving"]["paused_by_you"] == 1
    # With nothing owed, a paused plan keeps its diagnostic parts under Waiting.
    _lead, display = _header(nodes[1:], plan_state="paused")
    assert display == {
        "label_key": "waiting", "tone_key": "waiting",
        "not_moving": _parts(paused_by_you=1),
    }


def test_a_container_row_keeps_its_own_precedence():
    counts = {**dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0), "waiting": 2}
    assert operator_nodes.branch_display(
        counts, asks_user=False, working=False,
    )["label_key"] == "waiting"


# --- chat list ----------------------------------------------------------------


def _counts(**buckets):
    return {**dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0), **buckets}


@pytest.mark.parametrize(("counts", "stuck", "state"), [
    (_counts(waiting=1), _parts(ends=SOON), "idle"),
    # A user-owned hold alone is no one-tap ask, so it never marks the row (#2638).
    (_counts(blocked=1, waiting=1), _parts(need_you=1), "idle"),
    (_counts(paused=2), _parts(paused_by_you=2), "paused"),
    (_counts(paused=2), _parts(paused_by_you=1), "idle"),
    (_counts(paused=1, waiting=1), _parts(paused_by_you=1, ends=SOON), "idle"),
    # The user's ready task is no one-tap ask either (#2638), and is no pause.
    (_counts(pending_ready=1), _parts(need_you=1), "idle"),
    (_counts(pending_ready=1, paused=1), _parts(need_you=1, paused_by_you=1), "idle"),
])
def test_a_still_plan_never_reads_moving_in_the_chat_list(counts, stuck, state):
    plan = {"state": "active"}
    assert operator_chat_state.chat_state(plan, counts, stuck=stuck) == state


def _future(hours: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _block(db, plan_id, node_id, actor, owner_kind, **extra):
    plan_update(db, plan_id, "block_node", {
        "node_id": node_id, "reason_key": "needs_access", "reason": "Needs access",
        "owner_kind": owner_kind, "owner_id": None,
        "expected_revision": revision(db, node_id), **extra,
    }, actor=actor)


def test_a_still_plan_with_a_one_tap_ask_is_blocked_on_you(db):
    from tests.test_plan_chat_list_state import _awaiting_user_review

    _thread(db, "asks")
    plan = _plan(db, "asks")
    _awaiting_user_review(db, plan.id)

    state = _states(db, ["asks"])["asks"]
    assert (state["state"], state["label_key"]) == ("blocked_on_you", "needs_you")


def test_the_chat_list_reads_the_header_parts(db):
    held_actor = _thread(db, "held")
    held = _plan(db, "held")
    _block(db, held.id, task(db, held.id, "Choose").id, held_actor, "user")

    waits_actor = _thread(db, "waits")
    waits = _plan(db, "waits")
    ends = _future(5)
    _block(
        db, waits.id, task(db, waits.id, "Access").id, waits_actor, "external",
        condition={"type": "deadline", "value": ends},
    )

    _thread(db, "rests")
    rests = _plan(db, "rests")
    rested = task(db, rests.id, "Later")
    plan_update(db, rests.id, "activate", {
        "node_id": rested.id, "expected_revision": revision(db, rested.id),
    }, actor=HUMAN)
    pause(db, rests.id, rested.id, actor=HUMAN)

    states = _states(db, ["held", "waits", "rests"])
    assert {key: value["state"] for key, value in states.items()} == {
        "held": "blocked_on_you", "waits": "waiting", "rests": "paused",
    }
    assert {value["label_key"] for value in states.values()} == {"needs_you", "waiting"}


def test_a_plan_holding_only_the_users_task_reads_not_moving_everywhere(db):
    actor = _thread(db, "theirs")
    plan = _plan(db, "theirs")
    user_task = task(db, plan.id, "Sign the contract", assignee="user")
    row = service._plan_row(db, plan.id)

    state = _states(db, ["theirs"])["theirs"]
    assert (state["state"], state["label_key"]) == ("blocked_on_you", "needs_you")
    assert state["attention_items"] == [{
        "kind": "hold", "title": "Sign the contract", "node_id": user_task.id,
    }]
    assert status.read(db, row, actor=actor)["severity"] == "not_moving"
    assert status_context.projection(db, row, actor=actor)["note"] == (
        "not moving: 1 needs the user. Pick up anything you can move; "
        "otherwise nothing is owed until something changes."
    )


# --- agent digest -------------------------------------------------------------


def test_the_digest_says_not_moving_with_the_same_parts(db):
    actor = _thread(db, "agent")
    plan = _plan(db, "agent")
    _block(db, plan.id, task(db, plan.id, "Choose").id, actor, "user")
    ends = _future(5)
    _block(
        db, plan.id, task(db, plan.id, "Access").id, actor, "external",
        condition={"type": "deadline", "value": ends},
    )
    row = service._plan_row(db, plan.id)

    # EW1 precedence: what the user owes outranks the outside wait on both reads.
    assert status.read(db, row, actor=actor)["severity"] == "not_moving"
    assert _states(db, ["agent"])["agent"]["state"] == "blocked_on_you"
    thin = status_context.projection(db, row, actor=actor)
    assert thin["severity"] == "not_moving"
    assert thin["note"] == (
        f"not moving: 1 needs the user · next wait ends {ends}. Pick up anything "
        f"you can move; otherwise nothing is owed until {ends}."
    )


def test_the_digest_note_drops_empty_parts():
    note = not_moving.note(_parts(paused_by_you=2))
    assert note == (
        "not moving: 2 paused by the user. Pick up anything you can move; "
        "otherwise nothing is owed until something changes."
    )


def test_the_digest_counts_self_set_aside_waits_from_the_watch_cause(db):
    actor = _thread(db, "parked-agent")
    plan = _plan(db, "parked-agent")
    parked = task(db, plan.id, "Resume the draft")
    plan_update(db, plan.id, "activate", {
        "node_id": parked.id, "expected_revision": revision(db, parked.id),
    }, actor=actor)
    ends = _future(5)
    plan_update(db, plan.id, "arm_watch", {
        "node_id": parked.id, "expected_revision": revision(db, parked.id),
        "condition": {"type": "deadline", "value": ends}, "reason": "After review",
    }, actor=actor)

    note = status_context.projection(db, service._plan_row(db, plan.id), actor=actor)["note"]

    assert note == (
        f"not moving: next wait ends {ends}. You scheduled 1 of these yourself; nothing releases "
        "them early unless you do. Review them in plan order and start any you can drive now."
    )


def test_the_digest_tells_the_agent_to_resume_its_self_set_aside_work():
    note = not_moving.note(_parts(ends=SOON, set_aside=2))
    assert note == (
        f"not moving: next wait ends {SOON}. You scheduled 2 of these yourself; nothing releases "
        "them early unless you do. Review them in plan order and start any you can drive now."
    )
    _lead, display = _header([_leaf("parked", "waiting", _wait(SOON, scheduled_reason="Later"))])
    assert display["not_moving"]["set_aside"] == 1


def test_the_digest_does_not_count_set_aside_work_that_something_else_also_holds():
    _lead, display = _header([
        _leaf("also-waits", "waiting", _wait(SOON, scheduled_reason="Later"), _wait(SOON)),
        _leaf("also-held", "blocked", _USER_HOLD, _wait(SOON, scheduled_reason="Later")),
    ])
    assert display["not_moving"]["set_aside"] == 0


# --- overdue waits (Product Experience AS6) ---------------------------------------------


def _ago(delta: timedelta) -> str:
    return (NOW - delta).isoformat(timespec="seconds")


def _overdue(nodes: list[dict]) -> int:
    return _header(nodes)[1]["not_moving"]["overdue_waits"]


def test_a_wait_past_its_deadline_by_more_than_the_grace_is_overdue():
    late = _ago(not_moving.OVERDUE_GRACE + timedelta(seconds=1))
    assert _overdue([_leaf("a", "waiting", _wait(late)), _leaf("b", "waiting", _wait(SOON))]) == 1


def test_a_wait_still_inside_the_grace_is_not_overdue():
    """The expiry wake needs time to reach the agent: just past the deadline is no fault."""
    assert _overdue([_leaf("a", "waiting", _wait(_ago(not_moving.OVERDUE_GRACE)))]) == 0


def test_a_wait_with_a_later_deadline_or_a_new_one_is_not_overdue():
    """An agent that re-armed left a future deadline beside the old one."""
    late = _ago(timedelta(days=1))
    assert _overdue([_leaf("a", "waiting", _wait(late), _wait(SOON))]) == 0


def test_a_wait_without_a_deadline_is_never_overdue():
    """Sequencing behind a prerequisite has no deadline and is not stuck."""
    assert _overdue([_leaf("a", "waiting", _wait(None)), _leaf("b", "waiting", _wait(SOON))]) == 0


def test_every_overdue_leaf_counts_once():
    late = _ago(timedelta(hours=8))
    assert _overdue([
        _leaf("a", "waiting", _wait(late), _wait(_ago(timedelta(hours=9)))),
        _leaf("b", "waiting", _wait(late)),
    ]) == 2


def test_next_overdue_at_is_when_the_first_dated_wait_turns_overdue():
    """A clock fact no write announces: surfaces re-read at this instant."""
    inside = _ago(timedelta(minutes=1))
    nodes = [
        _leaf("a", "waiting", _wait(SOON)), _leaf("b", "waiting", _wait(inside)),
        _leaf("c", "waiting", _wait(_ago(timedelta(days=1)))), _leaf("d", "waiting", _wait(None)),
    ]
    facts = _header(nodes)[1]["not_moving"]
    assert facts["overdue_waits"] == 1
    assert facts["next_overdue_at"] == (
        NOW - timedelta(minutes=1) + not_moving.OVERDUE_GRACE
    ).isoformat()
    assert _header([_leaf("d", "waiting", _wait(None))])[1]["not_moving"]["next_overdue_at"] is None


def test_the_digest_names_overdue_waits_and_what_to_do():
    text = not_moving.note(_parts(overdue=3, ends=_ago(timedelta(hours=8))))
    assert "3 waits are past the deadline" in text
    assert "nothing is owed" not in text
    for exit_ in ("resolve it", "wait again", "raise a decision", "close the task"):
        assert exit_ in text
    assert "1 wait is past the deadline" in not_moving.note(_parts(overdue=1))


def test_two_overdue_waits_keep_plan_order_and_name_each_task():
    first = _leaf("12345678-first", "waiting", _wait(_ago(timedelta(days=2))))
    first["title"] = "First overdue task"
    second = _leaf("87654321-second", "waiting", _wait(_ago(timedelta(days=1))))
    second["title"] = "Second overdue task"

    _lead, display = _header([first, second])

    facts = display["not_moving"]
    assert facts["overdue_waits"] == 2
    assert facts["overdue_wait_entries"] == [
        {"title": "First overdue task", "node_id": "12345678-first"},
        {"title": "Second overdue task", "node_id": "87654321-second"},
    ]
    assert not_moving.note(facts).endswith(
        "First overdue task (12345678-first), Second overdue task (87654321-second). Check each: "
        "resolve it if it cleared, wait again with a new deadline and a reason, raise a "
        "decision, or close the task."
    )


def test_many_overdue_waits_name_the_first_few_by_full_id_and_count_the_rest():
    """Full ids the agent can pass back; the line stays inside the checkpoint budget."""
    late = _ago(timedelta(days=1))
    nodes = []
    for index in range(operator_nodes.OPEN_NODE_BUDGET):
        leaf = _leaf(f"{index:032x}", "waiting", _wait(late))
        leaf["title"] = "T" * 60
        nodes.append(leaf)

    facts = _header(nodes)[1]["not_moving"]
    text = not_moving.note(facts)

    assert facts["overdue_waits"] == operator_nodes.OPEN_NODE_BUDGET
    named = [f"({index:032x})" for index in range(not_moving.NAMED_OVERDUE)]
    assert all(full_id in text for full_id in named)
    assert f"({not_moving.NAMED_OVERDUE:032x})" not in text
    hidden = operator_nodes.OPEN_NODE_BUDGET - not_moving.NAMED_OVERDUE
    assert f" and {hidden} more. Check each:" in text
    assert len(status_context.render({"note": text}).encode()) < status_context.CHECKPOINT_BYTES // 3


def test_named_overdue_titles_are_cut_by_emitted_bytes_in_any_script():
    """Non-ASCII titles escape to 6 or 12 bytes a character; the note still fits."""
    late = _ago(timedelta(days=1))
    nodes = []
    for index, title in enumerate(("\U0001F600" * 60, "漢" * 60, "\U0001F680" * 60, "x")):
        leaf = _leaf(f"{index:032x}", "waiting", _wait(late))
        leaf["title"] = title
        nodes.append(leaf)

    text = not_moving.note(_header(nodes)[1]["not_moving"])

    assert "漢" in text and "(00000000000000000000000000000002)" in text
    assert len(status_context.render({"note": text}).encode()) < status_context.CHECKPOINT_BYTES // 3


def test_unnamed_facts_count_overdue_waits_without_an_empty_list():
    """Aggregate facts (Activity) carry the count only; the note names no one."""
    counts = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)
    counts["waiting"] = 1
    late = _ago(timedelta(days=1))
    facts = not_moving.read_from_facts(
        counts, user_ready=0, user_blocked=0, paused_by_user=0, wait_deadlines=[(late, late, None)],
    )

    assert facts is not None and facts["overdue_waits"] == 1
    assert "overdue_wait_entries" not in facts
    assert "1 wait is past the deadline. Check each:" in not_moving.note(facts)
