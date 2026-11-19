"""Flat-plan shortcuts retain the canonical folding and distribution rules."""

from plans import node_states, operator_node_state, operator_nodes
from tests.test_plan_activity_traversal_equivalence import row


def test_flat_fold_includes_partial_parent_and_excludes_archived():
    rows = [row("done", "accepted"), row("open", "active"),
            row("parent", "pending", child=True), row("archived", "accepted", archived="date")]
    parents = node_states.parent_ids(rows)
    assert node_states.folded_ids(rows, with_children=parents) == {"done", "parent"}
    leaves = node_states.leaves(rows, parents=parents)
    assert [item["id"] for item in node_states.in_play(rows, leaf_rows=leaves, with_children=parents)] == ["open"]


def test_flat_distribution_counts_branches_and_partial_parents():
    rows = [row("open", "active"), row("done", "accepted"),
            row("parent", "pending", child=True), row("archived", "accepted", archived="date")]
    buckets = {"open": "active", "done": "done", "parent": "active", "archived": "done"}
    work = node_states.work_ids(rows)
    holders = node_states.parent_ids(rows, work=work)
    branches, counts = operator_nodes.distribution_from_buckets(rows, buckets, work=work, holders=holders)
    zero = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)
    assert branches == {"open": {**zero, "active": 1}, "done": {**zero, "done": 1},
                        "parent": zero, "archived": zero}
    assert counts == {**zero, "active": 1, "done": 1}
