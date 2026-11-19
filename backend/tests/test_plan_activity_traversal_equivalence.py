"""Activity's shared traversal facts preserve the canonical node summaries."""

from types import SimpleNamespace

import pytest

from plans import activity_reducer, node_states, operator_nodes


def row(node_id, state, parent=None, *, child=False, archived=None, step=None):
    return {
        "id": node_id, "state": state, "parent_id": parent,
        "spawned_thread_id": None, "is_parent": child,
        "archived_at": archived, "kind": "task", "workflow_step": step,
    }


@pytest.mark.parametrize("rows", [
    [],
    [row("a", "active"), row("b", "accepted"), row("c", "failed"),
     row("d", "closed"), row("e", "pending", child=True),
     row("f", "active", archived="2026-01-01")],
    [row("p", "pending"), row("a", "active", "p"),
     row("b", "failed", "p"), row("c", "closed"), row("d", "active", "c")],
    [row("orphan", "active", "missing"), row("leaf", "review_requested")],
    [row("root", "pending"), row("partial", "pending", "root", child=True),
     row("done", "accepted", "root"), row("archived", "active", "root", archived="date")],
    # A workflow task counts only open children: a settled one leaves it a leaf.
    [row("step", "active", step="check"), row("settled", "accepted", "step"),
     row("open_step", "active", step="check"), row("running", "active", "open_step")],
])
def test_shared_traversal_facts_match_canonical_summaries(rows):
    work = node_states.work_ids(rows)
    parents = node_states.parent_ids(rows, work=work)
    leaves = node_states.leaves(rows, work=work, parents=parents)
    buckets = {item["id"]: "active" for item in rows}
    assert leaves == node_states.leaves(rows)
    assert node_states.in_play(rows, leaf_rows=leaves, with_children=parents) == node_states.in_play(rows)
    reduced = {node_id: SimpleNamespace(bucket=bucket) for node_id, bucket in buckets.items()}
    full = operator_nodes.distribution_from_buckets(rows, buckets)[1]
    assert operator_nodes.distribution_from_buckets(
        rows, buckets, work=work, holders=parents, include_branches=False,
    ) == ({}, full)
    assert activity_reducer.distribution_counts(rows, reduced) == (leaves, parents, full)


def test_nested_cycle_still_raises():
    rows = [row("a", "active", "b"), row("b", "active", "a")]
    reduced = {node_id: SimpleNamespace(bucket="active") for node_id in ("a", "b")}
    with pytest.raises(ValueError, match="cycle"):
        activity_reducer.distribution_counts(rows, reduced)
