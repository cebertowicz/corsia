"""``stored_mismatches`` covers every stored aggregate column, each with a mutant (#731 S1b-2).

The recount compares the subtree sums and fold-row counters; this file pins the
columns it used to leave to the property test: ``listed``, ``plans.top_roots``,
``own_last_changed``, ``folded_at`` and ``fold_bucket``. Each test starts from a
plan the real operations built (so it is consistent), corrupts one stored value
the way a missed hook call would, and requires the named mismatch; the plan is
rebuilt afterwards so the teardown recount judges the rows and not the mutant.

``fold_bucket`` rule found in ``aggregates.refresh``: a node that is folded and
not archived and not a sent-back round stores its tone (``done`` / ``failed`` /
``closed``, ``foldLabel``'s bucket); every other node stores ``None``.
"""

from __future__ import annotations

import pytest

from plans.aggregates_rebuild import rebuild
from plans.aggregates_recount import stored_mismatches
from tests.test_plan_aggregates_property import World


class Scene:
    """A plan with a folded phase, an open task, an archived task and a sent-back round."""

    def __init__(self) -> None:
        self.world = world = World()
        self.phase = world.add(None, "phase")
        self.done = world.add(self.phase)
        self.open = world.add(None)
        self.archived = world.add(None)
        self.sent_back = world.add(None)
        assert self.phase and self.done and self.open and self.archived and self.sent_back
        assert world.start(self.done) and world.review(self.done) and world.accept(self.done)
        assert world.archive(self.archived)
        assert world.start(self.sent_back) and world.review(self.sent_back)
        assert world.reject(self.sent_back)  # a send-back closes the round
        self.db = world.db
        self.plan = world.plan.id
        assert stored_mismatches(self.db, self.plan) == []

    def mismatches_after(self, sql: str, *params: object) -> list[str]:
        """Corrupt with one statement, report what the recount finds, then repair the plan."""
        try:
            self.db.execute(sql, params)
            return stored_mismatches(self.db, self.plan)
        finally:
            rebuild(self.db, self.plan)
            self.db.commit()


@pytest.fixture
def scene() -> Scene:
    return Scene()


def test_the_consistent_scene_stores_the_rule_the_oracle_names(scene: Scene) -> None:
    rows = {row["id"]: row for row in scene.db.execute("SELECT * FROM plan_nodes")}
    assert rows[scene.phase]["fold_bucket"] == "done" and rows[scene.phase]["folded_at"] is not None
    assert rows[scene.done]["fold_bucket"] == "done"
    assert rows[scene.open]["fold_bucket"] is None and rows[scene.open]["folded_at"] is None
    assert rows[scene.archived]["fold_bucket"] is None and rows[scene.archived]["listed"] == 0
    assert rows[scene.sent_back]["fold_bucket"] is None and rows[scene.sent_back]["sent_back"] == 1


def test_listed_must_follow_archived_at(scene: Scene) -> None:
    live = scene.mismatches_after("UPDATE plan_nodes SET listed=0 WHERE id=?", scene.open)
    assert live == [f"listed:{scene.open}"]
    archived = scene.mismatches_after("UPDATE plan_nodes SET listed=1 WHERE id=?", scene.archived)
    assert archived == [f"listed:{scene.archived}"]


def test_top_roots_must_count_the_listed_parentless_rows(scene: Scene) -> None:
    roots = scene.db.execute(
        "SELECT COUNT(*) FROM plan_nodes WHERE plan_id=? AND parent_id IS NULL AND archived_at IS NULL",
        (scene.plan,),
    ).fetchone()[0]
    assert roots == 4  # phase, open task, the sent-back round and the node that replaced it
    assert scene.db.execute("SELECT top_roots FROM plans WHERE id=?", (scene.plan,)).fetchone()[0] == roots
    for stored in (roots - 1, roots + 1):
        assert scene.mismatches_after("UPDATE plans SET top_roots=? WHERE id=?", stored, scene.plan) == ["top_roots"]


def test_own_last_changed_must_not_be_null(scene: Scene) -> None:
    for node in (scene.open, scene.archived):
        found = scene.mismatches_after("UPDATE plan_nodes SET own_last_changed=NULL WHERE id=?", node)
        assert found == [f"own_last_changed:{node}"]


def test_folded_at_is_set_exactly_when_the_node_is_folded(scene: Scene) -> None:
    unset = scene.mismatches_after("UPDATE plan_nodes SET folded_at=NULL WHERE id=?", scene.phase)
    assert unset == [f"folded_at:{scene.phase}"]
    stray = scene.mismatches_after("UPDATE plan_nodes SET folded_at='2026-01-01' WHERE id=?", scene.open)
    assert stray == [f"folded_at:{scene.open}"]


def test_fold_bucket_is_the_oracle_tone_for_a_folded_listed_node_else_none(scene: Scene) -> None:
    wrong = scene.mismatches_after("UPDATE plan_nodes SET fold_bucket='failed' WHERE id=?", scene.phase)
    assert wrong == [f"fold_bucket:{scene.phase}"]
    missing = scene.mismatches_after("UPDATE plan_nodes SET fold_bucket=NULL WHERE id=?", scene.done)
    assert f"fold_bucket:{scene.done}" in missing
    on_open = scene.mismatches_after("UPDATE plan_nodes SET fold_bucket='done' WHERE id=?", scene.open)
    assert f"fold_bucket:{scene.open}" in on_open
    on_archived = scene.mismatches_after("UPDATE plan_nodes SET fold_bucket='closed' WHERE id=?", scene.archived)
    assert f"fold_bucket:{scene.archived}" in on_archived
    on_sent_back = scene.mismatches_after("UPDATE plan_nodes SET fold_bucket='closed' WHERE id=?", scene.sent_back)
    assert f"fold_bucket:{scene.sent_back}" in on_sent_back
