"""Mutation keys of the plan writes CORSIA makes for an agent, not the agent's own.

Each automated writer builds its key here, and ``audit_plan_activity``
excludes exactly these keys from an agent's ``plan_update`` calls, reading
the patterns from the same builders, so the two cannot drift apart.
"""

from __future__ import annotations


def attach(via: str, child_thread_id: str, node_id: str) -> str:
    """A delegation tool attaching its worker; a reused worker gets several tasks, so both are named."""
    return f"{via}-attach-{child_thread_id}-{node_id}"


def first_run_start(conductor_id: str) -> str:
    """First run starting the new conductor's opening task."""
    return f"first-run-start:{conductor_id}"


def first_run_app_reconcile(conductor_id: str, node_id: str) -> str:
    """First run archiving an app-setup step the install no longer needs."""
    return f"first-run-app-reconcile:{conductor_id}:{node_id}"


def first_run_app_order(conductor_id: str, node_id: str, revision: int | str) -> str:
    """First run putting the app-setup steps back in order."""
    return f"first-run-app-order:{conductor_id}:{node_id}:{revision}"


def excluded() -> tuple[tuple[str, str], ...]:
    """``(operation, GLOB)`` for every key above: each builder with every part a wildcard."""
    return (
        ("attach_spawned_thread", attach("*", "*", "*")),
        ("start", first_run_start("*")),
        ("archive_node", first_run_app_reconcile("*", "*")),
        ("reorder", first_run_app_order("*", "*", "*")),
    )
