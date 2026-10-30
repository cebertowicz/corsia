"""A branch folds the moment nothing in it is open; the pane and the agent fold the same set.

Gate 3 (decision 483): a node folds when it is settled (done, failed or
closed), or is a container nobody started, and every node beneath it folds
too. There is no time window. Nothing is written: the fold is a pure function
of each node's state, ``archived_at``, children and worker. The operator
projection marks each node ``folded``; the agent's full read omits folded
nodes unless it passes ``include_folded`` and says, per level, how many it
left out. Progress counts only top-level branches that have not folded.
"""

from __future__ import annotations

import pytest

from plans import (
    archival, node_states, operator_node_state, operator_nodes, operator_plan_display,
    operator_projection, pane_views,
    service,
)
from plans.boundary import PlanActor
from plans.current import read_current_plan, read_current_status
from plans.errors import PlanInputError
from tests.test_plan_status import WORKER, _db, _task
from tools.plans import PlanReadTool

HUMAN = PlanActor(None, "user-1", None, kind="user")


def _phase(db, plan_id: str, title: str):
    return service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} done", db=db,
    )


def _mixed_plan(db):
    """An open phase with one finished step, a finished phase, finished roots, open work."""
    plan = service.ensure_plan("thread", "worker", db=db)
    open_phase = _phase(db, plan.id, "Open phase")
    done_phase = _phase(db, plan.id, "Finished phase")
    done_child = _task(db, plan.id, "Finished step", state="accepted", parent_id=open_phase.id)
    open_child = _task(db, plan.id, "Open step", parent_id=open_phase.id)
    accepted = _task(db, plan.id, "Phase success", state="accepted", parent_id=done_phase.id)
    failed = _task(db, plan.id, "Phase failure", state="failed", parent_id=done_phase.id)
    roots = [_task(db, plan.id, f"Finished root {i}", state="accepted") for i in range(3)]
    open_root = _task(db, plan.id, "Open root")
    return plan, {
        "open_phase": open_phase, "done_phase": done_phase, "done_child": done_child,
        "open_child": open_child, "accepted": accepted, "failed": failed,
        "roots": roots, "open_root": open_root,
    }


def _folded(nodes) -> set[str]:
    return {nodes["done_child"].id, nodes["done_phase"].id, nodes["accepted"].id,
            nodes["failed"].id, *(root.id for root in nodes["roots"])}


# --- the predicate ------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(node_states._TERMINAL_STATES))
def test_every_terminal_state_folds_at_once(state):
    assert node_states.folded(state, None, container=False) is True


@pytest.mark.parametrize(
    ("state", "archived_at", "container", "expected"),
    [
        ("accepted", "2026-09-01T00:00:00+00:00", False, False),
        ("active", None, False, False),
        ("pending", None, False, False),
        ("pending", None, True, True),
        ("review_requested", None, False, False),
    ],
)
def test_fold_needs_settled_work_or_an_unstarted_container(state, archived_at, container, expected):
    assert node_states.folded(state, archived_at, container=container) is expected


def _row(node_id, parent=None, state="accepted", spawned=None):
    return {"id": node_id, "parent_id": parent, "state": state, "spawned_thread_id": spawned}


def test_an_unstarted_container_folds_with_its_finished_children():
    rows = [_row("p", state="pending"), _row("c1", "p", "closed"), _row("c2", "p", "failed")]
    assert node_states.folded_ids(rows) == {"p", "c1", "c2"}


def test_a_started_parent_keeps_its_own_obligation():
    rows = [_row("p", state="active"), _row("c", "p")]
    assert node_states.folded_ids(rows) == {"c"}


def test_a_parent_with_a_worker_is_not_a_container():
    rows = [_row("p", state="pending", spawned="worker-2"), _row("c", "p")]
    assert node_states.folded_ids(rows) == {"c"}


def test_an_open_descendant_blocks_every_ancestor():
    rows = [
        _row("p", state="pending"), _row("q", "p", "pending"),
        _row("leaf", "q", "pending"), _row("done", "p"),
    ]
    assert node_states.folded_ids(rows) == {"done"}


def test_a_finished_parent_never_folds_over_open_work():
    parents = {"phase": None, "done": "phase", "open": "phase", "leaf": "done"}
    own = {"phase": True, "done": True, "open": False, "leaf": True}
    assert node_states.folded_subtrees(parents, own) == {"done", "leaf"}


def test_a_cyclic_parent_chain_terminates():
    assert node_states.folded_subtrees({"a": "b", "b": "a"}, {"a": False, "b": True}) == set()


def test_only_an_unstarted_parent_with_no_worker_is_a_container():
    assert node_states.is_container({"state": "pending", "spawned_thread_id": None}, True)
    assert not node_states.is_container({"state": "pending", "spawned_thread_id": None}, False)
    assert not node_states.is_container({"state": "active", "spawned_thread_id": None}, True)
    assert not node_states.is_container({"state": "pending", "spawned_thread_id": "w"}, True)


def _counts(**buckets):
    return {**dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0), **buckets}


#: Internal lifecycle precedence remains exact while both human surfaces use
#: the same four-state vocabulary.
_PRECEDENCE = [
    (_counts(review_human=1, review_parent_agent=1, active=1, blocked=1), "review_human", "working"),
    (_counts(review_parent_agent=1, active=1, blocked=1), "review_parent_agent", "working"),
    (_counts(active=1, blocked=1, done=1), "active", "working"),
    (_counts(blocked=1, waiting=1, pending_ready=1), "blocked", "working"),
    (_counts(waiting=2, done=1), "waiting", "waiting"),
    (_counts(paused=2, failed=1), "paused", "waiting"),
    (_counts(waiting=1, paused=1, done=1), "open", "waiting"),
    (_counts(failed=1, done=1), "failed", "finished"),
    (_counts(done=2, closed=1), "done", "finished"),
]


@pytest.mark.parametrize(("counts", "lead", "display_label"), _PRECEDENCE)
def test_a_container_and_the_header_share_one_precedence(counts, lead, display_label):
    assert operator_node_state.leading(counts) == lead
    needs_user = counts["review_human"] > 0
    working = any(counts[key] for key in ("review_parent_agent", "active", "pending_ready"))
    branch = operator_nodes.branch_display(
        counts, asks_user=needs_user, working=working,
    )
    assert branch["label_key"] == display_label
    node = {
        "id": "leaf", "parent_id": None, "state": "active",
        "branch_progress": None, "display": branch,
        "requires_user_action": needs_user,
        "execution": {"condition": "ready", "causes": []}, "review": None,
    }
    plan = {"state": "active"}
    assert operator_plan_display.plan_display(plan, counts, [node])["label_key"] == display_label


def test_moving_work_outranks_a_block_on_a_container_row_too():
    """#2190 review: the row read Blocked while the header read Advancing."""
    counts = _counts(active=1, blocked=1)
    assert operator_nodes.branch_display(
        counts, asks_user=False, working=True,
    )["tone_key"] == "active"
    row = {
        "id": "moving", "parent_id": None, "state": "active", "branch_progress": None,
        "display": {"label_key": "working", "tone_key": "active"},
        "requires_user_action": False,
        "execution": {"condition": "running", "causes": []}, "review": None,
    }
    assert operator_plan_display.plan_display(
        {"state": "active"}, counts, [row],
    )["tone_key"] == "active"


# --- the pane's projection ----------------------------------------------------


def test_the_operator_projection_marks_folded_and_counts_branches_in_play():
    db = _db()
    try:
        _plan, nodes = _mixed_plan(db)
        entry = operator_projection.read_thread("worker", actor=HUMAN, db=db)["plans"][0]
        by_id = {node["id"]: node for node in entry["nodes"]}
        # The pane lists the folded rows of the window at the levels it lists; the finished
        # phase's two children are behind its fold, counted but not listed.
        behind = {nodes["accepted"].id, nodes["failed"].id}
        assert {node_id for node_id, node in by_id.items() if node["folded"]} == _folded(nodes) - behind
        levels = {level["parent_id"]: level for level in entry["folds"]}
        assert levels[nodes["done_phase"].id] == {
            "parent_id": nodes["done_phase"].id, "done": 1, "failed": 1, "closed": 0, "more": 2,
        }
        assert levels[nodes["open_phase"].id]["more"] == 0 and levels[None]["more"] == 0
        assert entry["root_count"] == 6
        # Only the open phase and the open root are in play: 1 of 3 leaves done.
        assert (entry["progress"]["done"], entry["progress"]["total"]) == (1, 3)
        assert by_id[nodes["open_phase"].id]["branch_progress"] == {"done": 1, "total": 2}
        finished = by_id[nodes["done_phase"].id]
        assert finished["branch_progress"] == {"done": 1, "total": 2}
        # A container reads as its leaves; its raw lifecycle state is untouched.
        assert finished["display"]["label_key"] == "finished"
        assert finished["state"] == "pending"
        # The plan's buckets still cover every leaf, folded or not.
        distribution = entry["progress"]["distribution"]
        assert (distribution["done"], distribution["failed"]) == (5, 1)
    finally:
        db.close()


# --- what the reader has seen in the pane (plans.pane_views) ------------------


def _seen(db, user=HUMAN):
    return operator_projection.read_thread("worker", actor=user, db=db)["seen_through"]


def _view(db, seen, user=HUMAN):
    return operator_projection.record_thread_view("worker", seen_through=seen, actor=user, db=db)


def test_a_plan_never_seen_in_the_pane_has_no_watermark():
    db = _db()
    try:
        _mixed_plan(db)
        assert _seen(db) is None
    finally:
        db.close()


def test_opening_the_conversation_is_not_seeing_the_plan():
    # Chat presence says the user was in the thread, not that the pane showed the plan.
    db = _db()
    try:
        db.execute(
            """INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,
                 last_exited_at,last_confirmed_at,updated_at)
               VALUES ('user-1','worker',0,'2026-09-24 10:00:00','2026-09-24 10:20:00',
                 '2026-09-24 10:00:00','2026-09-24 10:20:00')""",
        )
        assert _seen(db) is None
    finally:
        db.close()


def test_a_view_is_read_back_at_one_fixed_width():
    db = _db()
    try:
        stored = _view(db, "2026-09-24T10:20:00.5Z")
        assert stored == {"seen_through": "2026-09-24T10:20:00.500000+00:00"}
        assert _seen(db) == stored["seen_through"]
    finally:
        db.close()


def test_the_watermark_only_moves_forward():
    # Two open panes report in any order; the older report never unsees work.
    db = _db()
    try:
        _view(db, "2026-09-24T10:20:00Z")
        _view(db, "2026-09-24T10:10:00Z")
        assert _seen(db) == "2026-09-24T10:20:00.000000+00:00"
    finally:
        db.close()


def test_the_future_is_never_seen():
    db = _db()
    try:
        assert _view(db, "2999-01-01T00:00:00Z")["seen_through"] < "2999"
    finally:
        db.close()


def test_another_readers_view_is_not_this_readers():
    db = _db()
    try:
        other = PlanActor(None, "user-2", None, kind="user")
        db.execute("UPDATE threads SET user_id='user-2' WHERE id='worker'")
        _view(db, "2026-09-24T10:20:00Z", user=other)
        db.execute("UPDATE threads SET user_id='user-1' WHERE id='worker'")
        assert _seen(db) is None
    finally:
        db.close()


def test_a_view_refuses_what_it_cannot_record():
    db = _db()
    try:
        with pytest.raises(pane_views.PaneViewInputError):
            _view(db, "yesterday")
        with pytest.raises(pane_views.PaneViewInputError):
            _view(db, "2026-09-24T10:20:00")
        with pytest.raises(operator_projection.OperatorNotFound):
            _view(db, "2026-09-24T10:20:00Z", user=PlanActor(None, "stranger", None, kind="user"))
        assert _seen(db) is None
    finally:
        db.close()


def test_a_view_moves_the_revision_token():
    # The thread read answers If-None-Match with 304; a watermark outside the
    # token would leave a returning user's pane on the previous view's value.
    db = _db()
    try:
        _mixed_plan(db)
        before = operator_projection.read_thread("worker", actor=HUMAN, db=db)["revision_token"]
        _view(db, "2026-09-24T10:20:00Z")
        after = operator_projection.read_thread("worker", actor=HUMAN, db=db)["revision_token"]
        assert before != after
    finally:
        db.close()


def test_scope_reads_carry_no_watermark():
    db = _db()
    try:
        _mixed_plan(db)
        _view(db, "2026-09-24T10:20:00Z")
        workspace = operator_projection.read_workspace(actor=HUMAN, db=db)
        assert workspace["seen_through"] is None
        assert "folded_through" not in workspace
    finally:
        db.close()


def test_a_fully_finished_plan_reads_zero_of_zero_and_keeps_its_nodes():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        _task(db, plan.id, "Only step", state="accepted")
        entry = operator_projection.read_thread("worker", actor=HUMAN, db=db)["plans"][0]
        assert (entry["progress"]["done"], entry["progress"]["total"]) == (0, 0)
        assert [node["folded"] for node in entry["nodes"]] == [True]
        assert entry["display"]["label_key"] == "finished"
    finally:
        db.close()


# --- the agent's full read ----------------------------------------------------


def test_the_full_read_omits_folded_nodes_and_counts_them_per_level():
    db = _db()
    try:
        _plan, nodes = _mixed_plan(db)
        page = read_current_plan(actor=WORKER, db=db)
        shown = {node["id"] for node in page["nodes"]}
        assert shown == {nodes["open_phase"].id, nodes["open_child"].id, nodes["open_root"].id}
        assert page["total_count"] == 3
        # Per level, by the tone the pane's fold row shows; a level with none left out, and
        # the finished phase reads as its leaves (one failed).
        levels = {
            level["parent_id"]: {tone: n for tone, n in level.items() if tone != "parent_id"}
            for level in page["folded"]["levels"]
        }
        assert levels == {None: {"done": 3, "failed": 1}, nodes["open_phase"].id: {"done": 1}}
        assert "node_count" not in page["folded"] and "count" not in page["folded"]["levels"][0]
        assert page["folded"]["include"] == "plan_read(mode='full', include_folded=true)"
        # The ratio the agent reads is the pane's ratio.
        assert (page["progress"]["done"], page["progress"]["total"]) == (1, 3)
    finally:
        db.close()


def test_include_folded_returns_every_node_marked():
    db = _db()
    try:
        _plan, nodes = _mixed_plan(db)
        page = read_current_plan(actor=WORKER, db=db, include_folded=True)
        assert page["total_count"] == 10
        marked = {node["id"] for node in page["nodes"] if node.get("folded")}
        assert marked == _folded(nodes)
        assert "folded" not in page
    finally:
        db.close()


def test_a_plan_with_only_open_work_reads_as_before():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        _task(db, plan.id, "Open")
        _task(db, plan.id, "Started", state="active")
        page = read_current_plan(actor=WORKER, db=db)
        assert page["total_count"] == 2
        assert "folded" not in page
        assert all("folded" not in node for node in page["nodes"])
    finally:
        db.close()


def test_an_include_folded_cursor_continues_with_the_folded_set():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        for i in range(60):
            _task(db, plan.id, f"Finished earlier, step number {i:02d}", state="accepted")
        _task(db, plan.id, "Still open")
        first = read_current_plan(actor=WORKER, db=db, include_folded=True)
        assert "include_folded=true" in first["continue_read"]
        pages = [first]
        while "cursor" in pages[-1]:
            pages.append(read_current_plan(actor=WORKER, db=db, cursor=pages[-1]["cursor"]))
        assert sum(len(p["nodes"]) for p in pages) == 61
        with pytest.raises(PlanInputError, match="include_folded"):
            read_current_plan(
                actor=WORKER, db=db, cursor=first["cursor"], include_folded=False,
            )
    finally:
        db.close()


def test_reading_writes_nothing():
    db = _db()
    try:
        plan, _nodes = _mixed_plan(db)
        revision = service._plan_row(db, plan.id)["revision"]
        before = db.total_changes
        read_current_plan(actor=WORKER, db=db)
        read_current_plan(actor=WORKER, db=db, include_folded=True)
        operator_projection.read_thread("worker", actor=HUMAN, db=db)
        assert db.total_changes == before
        assert service._plan_row(db, plan.id)["revision"] == revision
    finally:
        db.close()


# --- the other reads ----------------------------------------------------------


def test_a_folded_node_still_reads_in_full_by_id():
    db = _db()
    try:
        _plan, nodes = _mixed_plan(db)
        detail = read_current_status(actor=WORKER, db=db, node_id=nodes["failed"].id)
        assert detail["id"] == nodes["failed"].id
        assert detail["folded"] is True
        assert detail["acceptance_criteria"]
    finally:
        db.close()


def test_the_status_read_does_not_take_include_folded():
    db = _db()
    try:
        _mixed_plan(db)
        with pytest.raises(PlanInputError, match="mode='full'"):
            read_current_status(actor=WORKER, db=db, include_folded=True)
        status = read_current_status(actor=WORKER, db=db)
        assert "folded" not in status
    finally:
        db.close()


def test_an_archived_node_is_gone_not_folded():
    db = _db()
    try:
        _plan, nodes = _mixed_plan(db)
        target = nodes["roots"][0]
        archival.archive_node(target.id, actor_id="worker", db=db)
        db.commit()
        page = read_current_plan(actor=WORKER, db=db)
        assert page["folded"]["levels"][0] == {"parent_id": None, "done": 2, "failed": 1}
        entry = operator_projection.read_thread("worker", actor=HUMAN, db=db)["plans"][0]
        assert target.id not in {node["id"] for node in entry["nodes"]}
    finally:
        db.close()


def test_the_tool_schema_names_the_include_parameter():
    params = PlanReadTool().parameters["properties"]
    assert params["include_folded"]["type"] == "boolean"
    description = params["include_folded"]["description"]
    assert "mode='full'" in description and "folded" in description


# --- known_revision -----------------------------------------------------------


def test_a_fold_moves_the_revision_so_unchanged_means_the_same_folded_set():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        node = _task(db, plan.id, "About to finish", state="review_requested")
        _task(db, plan.id, "Open")
        first = read_current_plan(actor=WORKER, db=db)
        assert "folded" not in first
        service.transition_node(
            node.id, "accepted", actor_id="worker", verification_result="passed",
            verified_by="worker", db=db,
        )
        db.commit()
        second = read_current_plan(actor=WORKER, db=db, known_revision=first["plan"]["revision"])
        assert "unchanged" not in second
        assert second["folded"]["levels"] == [{"parent_id": None, "done": 1}]
    finally:
        db.close()


def test_known_revision_unchanged_is_just_the_plan():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        _task(db, plan.id, "Open")
        _task(db, plan.id, "Finished", state="accepted")
        first = read_current_plan(actor=WORKER, db=db)
        second = read_current_plan(actor=WORKER, db=db, known_revision=first["plan"]["revision"])
        assert second["unchanged"] is True
        assert second["plan"]["revision"] == first["plan"]["revision"]
        assert "folded" not in second
    finally:
        db.close()
