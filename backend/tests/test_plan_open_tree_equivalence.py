"""The open-work reads against the full-plan reads they replace (#731 S2-1).

``plans.status`` and ``plans.current`` read the open work and a recent window, and take
what they leave out from the stored aggregates. The full-plan projection stays as the
reference (``status._full_facts``, ``node_states``, ``aggregates_recount``): over random
sequences of the real plan operations, every output of the new reads must equal it,
whatever the clock cuts off of the settled history.

Sequences are seeded (``tests.test_plan_aggregates_property.run_sequence``);
``CORSIA_PLAN_EQUIVALENCE_SEQUENCES`` raises the count, and a failure names the seed.
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone

import pytest

from plans import aggregates_recount, current, node_order, node_states, open_tree, service, status
from plans.boundary import PlanActor
from plans.errors import PlanError
from tests.test_plan_aggregates_property import World, run_sequence

SEQUENCES = int(os.environ.get("CORSIA_PLAN_EQUIVALENCE_SEQUENCES", "120"))
ACTOR = PlanActor("owner", "user", "p")


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _clock(world: World, rng: random.Random) -> str:
    """A clock whose 30-day window starts at a random event of the plan (or past all of them)."""
    stamps = sorted(
        row[0] for row in world.db.execute(
            "SELECT e.created_at FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id"
            " WHERE n.plan_id=?", (world.plan.id,),
        )
    )
    if not stamps:
        return datetime.now(timezone.utc).isoformat()
    anchor = datetime.fromisoformat(rng.choice(stamps + [stamps[-1]]))
    return (anchor + timedelta(days=status.RECENT_DAYS, microseconds=rng.choice([0, 1]))).isoformat()


def _oracle(monkeypatch, db, plan_row, call):
    """``call`` with the status reads switched to the full-plan projection."""
    with monkeypatch.context() as scope:
        scope.setattr(
            status, "_facts",
            lambda db, plan_row, actor, clock, *, detail_id=None: status._full_facts(
                db, plan_row, actor, clock),
        )
        return call()


def _same_up_to_container_dates(left: dict, right: dict) -> None:
    """Every field equal, bar a container's last-changed time (see ``folded_changes``)."""
    for key in ("last_transition_at", "ages"):
        left.pop(key, None), right.pop(key, None)
    assert _dump(left) == _dump(right)


@pytest.mark.parametrize("block", range(4))
def test_status_reads_equal_the_full_plan_projection(monkeypatch, block: int) -> None:
    compared = 0
    for seed in range(block, SEQUENCES, 4):
        world = run_sequence(seed)
        world.db.commit()
        db, rng = world.db, random.Random(seed)
        plan_row = service._plan_row(db, world.plan.id)
        clock = _clock(world, rng)
        fresh = status.read(db, plan_row, actor=ACTOR, clock=clock)
        reference = _oracle(
            monkeypatch, db, plan_row,
            lambda: status.read(db, plan_row, actor=ACTOR, clock=clock),
        )
        assert _dump(fresh) == _dump(reference), f"seed {seed}: status overview"
        for section in status.SECTIONS:
            assert _dump(status.read(db, plan_row, actor=ACTOR, section=section, clock=clock)) == _dump(
                _oracle(monkeypatch, db, plan_row, lambda: status.read(
                    db, plan_row, actor=ACTOR, section=section, clock=clock)),
            ), f"seed {seed}: {section} page"
        for row in db.execute(
            "SELECT id,kind FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL", (world.plan.id,),
        ).fetchall():
            node_id = row["id"]
            fresh_detail = _detail_or_refusal(db, plan_row, node_id, clock)
            full_detail = _oracle(monkeypatch, db, plan_row, lambda: _detail_or_refusal(
                db, plan_row, node_id, clock))
            if isinstance(fresh_detail, str) or isinstance(full_detail, str):
                assert fresh_detail == full_detail, f"seed {seed}: detail {node_id}"
                continue
            _same_up_to_container_dates(fresh_detail, full_detail) if _is_container(db, node_id) else (
                _assert_equal(fresh_detail, full_detail, f"seed {seed}: detail {node_id}"))
            compared += 1
    assert compared


def _detail_or_refusal(db, plan_row, node_id: str, clock: str):
    """The detail, or the refusal text: a settled root the plan folds away is not in either read."""
    try:
        return status.node_detail(db, plan_row, node_id, actor=ACTOR, clock=clock)
    except PlanError as refusal:
        return str(refusal)


def _is_container(db, node_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM plan_nodes WHERE parent_id=? AND archived_at IS NULL LIMIT 1", (node_id,),
    ).fetchone() is not None


def _assert_equal(left: dict, right: dict, label: str) -> None:
    assert _dump(left) == _dump(right), label


def test_the_full_page_lists_the_open_rows_and_counts_the_fold_from_the_stored_tones() -> None:
    for seed in range(SEQUENCES):
        world = run_sequence(seed)
        world.db.commit()
        db = world.db
        plan_id = world.plan.id
        ordered = node_order.ordered_nodes(db, plan_id)
        fold = current.folded_ids(dict(row) for row in ordered)
        expected = [str(row["id"]) for row in ordered if row["id"] not in fold]
        tree = open_tree.load(db, plan_id)
        assert [str(row["id"]) for row in tree.rows] == expected, f"seed {seed}: listed rows"
        assert set(tree.shape.folded) == {
            str(row["id"]) for row in tree.rows if row["folded"]
        } == set(), f"seed {seed}: an open row reads folded"
        rows = [dict(row) for row in db.execute("SELECT * FROM plan_nodes WHERE plan_id=?", (plan_id,))]
        rejected = [
            str(row[0]) for row in db.execute(
                "SELECT DISTINCT e.node_id FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id"
                " WHERE n.plan_id=? AND e.event='review_rejected' AND n.state='closed'", (plan_id,),
            )
        ]
        counted = aggregates_recount.recount(rows, rejected)
        levels = []
        for parent, tones in [(None, counted.top_folded)] + [
            (node_id, counted.nodes[node_id].child_folded) for node_id in expected
        ]:
            if any(tones):
                levels.append({"parent_id": parent, **{
                    name: n for name, n in zip(("done", "failed", "closed"), tones) if n}})
        summary = current._folded_summary(tree)
        assert (summary["levels"] if summary else []) == levels, f"seed {seed}: fold levels"


def test_the_open_and_recent_window_reads_are_index_searches() -> None:
    world = run_sequence(0)
    window = open_tree._WINDOW_SQL.format(settled=",".join(f"'{s}'" for s in open_tree._SETTLED))
    for sql, params in ((open_tree._OPEN_SQL, (world.plan.id,)), (window, (world.plan.id, "2026-01-01"))):
        steps = [str(row[3]) for row in world.db.execute("EXPLAIN QUERY PLAN " + sql, params)]
        assert steps and not any(step.startswith("SCAN") for step in steps), steps
    assert any("idx_plan_nodes_recent" in step for step in steps), steps


def test_the_pane_rollup_equals_the_full_rollup_on_every_row_it_lists() -> None:
    """Over open rows plus an unbounded fold window, a listed row reads as the full rollup reads it.

    The pane lists the open work and, per level, the newest folded rows; a row beneath a
    folded parent is not listed (the folded route pages it). Every listed row must carry the
    full rollup's fields; the plan's progress and moving state must match too. The one
    difference is ``has_child_in_work``, a field only the pane's rows add: it must equal
    ``node_states.has_child_in_work_sql``.
    """
    from plans import open_rollup, rollup

    window = open_tree.FoldWindow(since=None, cap=10**6)
    compared = 0
    differing: dict[str, list[str]] = {}
    for seed in range(SEQUENCES):
        world = run_sequence(seed)
        world.db.commit()
        db = world.db
        fresh = open_rollup.rollup_thread(world.plan.id, db=db, window=window)
        full = rollup.rollup_thread(world.plan.id, db=db)
        by_id = {node["id"]: node for node in full.nodes}
        assert fresh.progress == full.progress, f"seed {seed}: progress"
        assert fresh.moving == full.moving, f"seed {seed}: moving"
        for node in fresh.nodes:
            expected = by_id.get(node["id"])
            assert expected is not None, f"seed {seed}: {node['id']} listed but not in the full rollup"
            in_work = db.execute(
                f"SELECT {node_states.has_child_in_work_sql('n')} FROM plan_nodes n WHERE n.id=?", (node["id"],),
            ).fetchone()[0]
            assert node["has_child_in_work"] == bool(in_work), f"seed {seed}: {node['id']} has_child_in_work"
            for key in sorted(set(node) - {"has_child_in_work"} | set(expected)):
                if _dump(node.get(key)) != _dump(expected.get(key)):
                    differing.setdefault(key, []).append(f"seed {seed} {node['id']}")
            compared += 1
    assert compared
    assert not differing, {key: (len(found), found[:3]) for key, found in differing.items()}


def test_paging_the_folded_routes_reaches_exactly_the_rows_the_full_projection_folds() -> None:
    """Walk the folded route from the root and from every open row: it finds each folded row once."""
    from plans import folded_reads

    human = PlanActor(None, "user", None, kind="user")
    reached_total = 0
    for seed in range(SEQUENCES):
        world = run_sequence(seed)
        world.db.commit()
        db = world.db
        plan_id = world.plan.id
        ordered = node_order.ordered_nodes(db, plan_id)
        expected = set(current.folded_ids(dict(row) for row in ordered))
        tree = open_tree.load(db, plan_id)
        reached: list[str] = []
        pending = [folded_reads.ROOT] + [str(row["id"]) for row in tree.rows]
        while pending:
            parent = pending.pop()
            cursor = None
            while True:
                page = folded_reads.read_folded(plan_id, parent=parent, cursor=cursor, actor=human, db=db)
                for item in page["items"]:
                    reached.append(item["id"])
                    if item["has_folded_children"] or item["folds"] != {"done": 0, "failed": 0, "closed": 0}:
                        pending.append(item["id"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        # A sent-back round is a closed row the plan hides beside its revision, in neither read.
        hidden = {str(row[0]) for row in db.execute(
            "SELECT id FROM plan_nodes WHERE plan_id=? AND sent_back=1", (plan_id,))}
        assert len(reached) == len(set(reached)), f"seed {seed}: a folded row appears twice"
        assert set(reached) == expected - hidden, f"seed {seed}: reached vs folded"
        reached_total += len(reached)
    assert reached_total
