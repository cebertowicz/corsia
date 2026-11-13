"""workflow_run_of answers the same whether its runs arrive as a list or a set."""
from __future__ import annotations

from core.correction_attribution import workflow_run_of


def test_run_ownership_is_the_same_for_a_list_and_a_set_of_runs() -> None:
    parents = {"a": None, "b": "a", "c": "b", "d": "c", "x": None, "y": "x", "loop1": "loop2", "loop2": "loop1"}
    runs = ["b", "x"]
    for node in [None, "a", "b", "c", "d", "x", "y", "loop1", "missing"]:
        assert workflow_run_of(node, parents, runs) == workflow_run_of(node, parents, frozenset(runs))
