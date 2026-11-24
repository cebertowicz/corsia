"""A run's open-work walk costs its own subtree, not the plan's settled history.

``run_end.open_nodes`` (run settlement) and ``run_end.close_run`` share one walk. It once
joined back to ``plan_nodes`` in an order that scanned every row of the table, so closing a
run cost the whole plan's history.
"""

from __future__ import annotations

from plans import run_end
from tests.plan_scale_fixture import build
from tests.test_plan_open_tree_scale import SAMPLE_EVERY, SIZES, STEP_TOLERANCE, measure


def test_run_open_walk_is_flat_in_settled_history() -> None:
    sizes: dict[int, tuple[int, int]] = {}
    for size in SIZES:
        scale = build(size)
        run_end.open_nodes(scale.db, scale.open_container)  # primes statement caches
        walk, _ = measure(scale.db, lambda: run_end.open_nodes(scale.db, scale.open_container))
        opened, _ = measure(scale.db, lambda: run_end._open_below(scale.db, scale.open_container))
        sizes[size] = (walk, opened)
        scale.db.close()
    small, large = min(sizes), max(sizes)
    print("\nrun open walk: " + ", ".join(f"{size}: {steps}" for size, steps in sorted(sizes.items())))
    for index in (0, 1):
        assert sizes[large][index] <= sizes[small][index] * STEP_TOLERANCE + 2 * SAMPLE_EVERY, sizes
