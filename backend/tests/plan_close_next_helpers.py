"""Scenes, readers and contract constants for closing-mutation `next` tests.

A closing operation (request_review, mark_done, fail, accept,
accept_with_follow_up, archive_node) returns a bounded `next` block naming the
executor's selected task and up to three of its other open tasks, and may start
one named task in the same transaction. The test modules named
`test_plan_close_next_*.py` share
this scaffolding so each reads as the scene it proves.

The constants below restate the `next` contract independently of production,
so a production constant that drifts fails a test instead of silently moving
the assertion with it.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from typing import Any

from plans import service
from plans.boundary import PlanActor, mutate
from tests.plan_ask_helpers import with_actionable_user_ask
from tests.plan_execution_helpers import add_task

#: The six operations that close the executor's task, and only these.
CLOSING_OPERATIONS = (
    "request_review", "mark_done", "fail", "accept", "accept_with_follow_up",
    "archive_node",
)
#: Hard serialized-byte ceiling for the whole block.
NEXT_BLOCK_CAP = 2048
#: Serialized-byte caps; a helper may count the JSON quotes or not, so the
#: assertions allow the two quote bytes on top of each cap.
TITLE_CAP, WAIT_CAP, REASON_CAP = 96, 72, 96
QUOTES = 2
#: The truncation marker appended after a cut label (title, wait, reason).
MARKER = "\u2026"
#: The marker that ends cut acceptance criteria. It cannot be mistaken for text
#: the author wrote, and it names the one call that returns the criteria whole.
CRITERIA_MARKER = "\u2026 [cut to fit; starting this task returns its full criteria]"
BLOCK_KEYS = frozenset({"executor_thread_id", "open_tasks", "open_tasks_listed", "selected"})
ROW_KEYS = frozenset({"id", "revision", "state", "title", "wait"})
SELECTED_KEYS = ROW_KEYS | {"how"}
NAMED_KEYS = frozenset({"reason", "refusal", "started"})
#: `selected.how` values that carry the selected task's acceptance criteria.
CRITERIA_HOWS = frozenset({"started", "auto_selected"})
EVENT_PROPERTY_KEYS = frozenset({
    "operation", "named", "selected_how", "open_total", "open_listed",
    "executor_is_actor", "idle",
    "description_carried", "description_bytes", "description_cut",
})
OWNER = PlanActor("owner", "user", None)
HUMAN = PlanActor(None, "user", None, kind="user")
_MISSING = object()


def listed(shown: int, total: int) -> str:
    """Return the self-describing `open_tasks_listed` label."""
    return f"{shown} of {total} other open tasks"


def idle_listed(shown: int, total: int, *, set_aside: int = 0) -> str:
    """Return the label of an idle executor's listing: nothing ready, self-set-aside work first."""
    order = "set-aside work first in plan order" if set_aside else "soonest-ending first"
    return f"{shown} of {total} open tasks, none ready, {order}"


def serialized(value: Any) -> str:
    """Serialize exactly as the plan tool emits its result."""
    return json.dumps(value, sort_keys=True)


def size(value: Any) -> int:
    """Return the serialized byte size of one value as the tool emits it."""
    return len(serialized(value))


def owner_plan(db):
    """Return the owner's thread plan."""
    return service.ensure_plan("thread", "owner", db=db)


def task(db, plan_id: str, title: str, **kwargs):
    """Add one pending task and commit it."""
    node = add_task(db, plan_id, title, **kwargs)
    db.commit()
    return node


def row(db, node_id: str):
    """Return one node row."""
    return service._node_row(db, node_id)


def revision(db, node_id: str) -> int:
    """Return a node's current revision."""
    return row(db, node_id)["revision"]


def plan_update(db, plan_id, operation, params, *, actor=OWNER, mutation_id=None) -> dict:
    """Commit one mutation through the real boundary and return its result."""
    params = with_actionable_user_ask(db, plan_id, operation, params, actor)
    return mutate(
        plan_id, operation, params, actor=actor, provenance="test.plan_close_next",
        mutation_id=mutation_id or uuid.uuid4().hex, db=db,
    ).result


def select(db, plan_id: str, node_id: str, *, actor=OWNER) -> str:
    """Activate one pending task into its free executor slot; return the attempt id."""
    result = plan_update(
        db, plan_id, "activate",
        {"node_id": node_id, "expected_revision": revision(db, node_id)}, actor=actor,
    )
    assert result["admission"]["condition"] == "running", result["admission"]
    return result["admission"]["selection"]["attempt_id"]


def queue_active(db, node_id: str) -> None:
    """Make one task active without selecting it (its slot is held elsewhere)."""
    service.transition_node(node_id, "active", db=db)
    db.commit()
    assert open_attempt(db, node_id) is None


def pause(
    db, plan_id: str, node_id: str, reason: str = "Operator paused the work", *, actor=OWNER,
) -> dict:
    """Place a pause hold on one active task.

    The user pauses through the boundary. An agent can no longer pause, so its
    pause is one placed before that, written by the runtime writer, which an
    open agent hold still is until the schema step schedules it.
    """
    if actor.kind == "user":
        return plan_update(
            db, plan_id, "pause_node",
            {"node_id": node_id, "reason": reason, "expected_revision": revision(db, node_id)},
            actor=actor,
        )
    from plans import holds

    result = holds.pause(
        node_id, reason, expected_revision=revision(db, node_id),
        actor_id=actor.audit_id, provenance="test.plan_close_next", db=db,
    )
    db.commit()
    return result


def request_review(db, plan_id: str, node_id: str) -> dict:
    """Submit review evidence for one active task through the boundary."""
    return plan_update(db, plan_id, "request_review", close_params(db, "request_review", node_id))


def close_params(db, operation: str, node_id: str, *, next_node_id: Any = _MISSING) -> dict:
    """Build a valid body for one closing operation at the node's current revision."""
    params: dict[str, Any] = {"node_id": node_id, "expected_revision": revision(db, node_id)}
    if operation in {"request_review", "mark_done"}:
        params["evidence"] = "Focused proof passed"
    if operation in {"accept", "accept_with_follow_up"}:
        params["verification_result"] = "passed"
    if operation == "accept_with_follow_up":
        params.update(title="Fix the follow-up", acceptance_criteria="Follow-up is verified")
    if next_node_id is not _MISSING:
        params["next_node_id"] = next_node_id
    return params


def close(
    db, plan_id: str, operation: str, node_id: str, *,
    next_node_id: Any = _MISSING, actor=OWNER, mutation_id=None,
) -> dict:
    """Apply one closing operation through the real boundary and return its result."""
    params = close_params(db, operation, node_id, next_node_id=next_node_id)
    return plan_update(db, plan_id, operation, params, actor=actor, mutation_id=mutation_id)


def open_attempt(db, node_id: str):
    """Return the node's open attempt row, or None."""
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


def attempt_count(db, node_id: str) -> int:
    """Return how many attempts a node has ever had."""
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=?", (node_id,),
    ).fetchone()[0]


def receipt_count(db) -> int:
    """Return how many mutation receipts exist."""
    return db.execute("SELECT COUNT(*) FROM plan_mutation_receipts").fetchone()[0]


def seed_dispatch(db, attempt_id: str, dispatch_id: str = "dispatch-in-flight") -> str:
    """Record one already-started, unsettled non-plan call on an attempt."""
    db.execute(
        "INSERT INTO plan_tool_dispatches (id,attempt_id,turn_id,tool_name,started_at) "
        "VALUES (?,?,?,?,?)",
        (dispatch_id, attempt_id, "turn-in-flight", "bash", service._now()),
    )
    db.commit()
    return dispatch_id


def next_block(result: dict) -> dict:
    """Return the `next` block of one closing result."""
    assert "next" in result, f"closing result has no next block: {sorted(result)}"
    return result["next"]


def row_ids(block: dict) -> list[str]:
    """Return the listed open-task ids in order."""
    return [item["id"] for item in block["open_tasks"]]


def assert_block_shape(block: dict, *, named: bool) -> None:
    """Assert the block, row, selected and named key sets of the `next` contract."""
    idle = "none ready" in block["open_tasks_listed"]
    expected = BLOCK_KEYS | ({"named"} if named else set())
    assert set(block) - {"idle"} == expected
    assert "idle" not in block or idle
    assert isinstance(block["open_tasks"], list)
    assert len(block["open_tasks"]) <= (5 if idle else 3)
    for item in block["open_tasks"]:
        keys = ROW_KEYS | ({"ends"} if idle else set())
        if "release_wait" in item:
            assert idle
            keys |= {"release_wait"}
            release = item["release_wait"]
            assert set(release) == {"operation", "params"}
            assert release["operation"] == "resolve_watch"
            assert set(release["params"]) == {"node_id", "watch_id", "expected_revision"}
        assert set(item) == keys
    selected = block["selected"]
    if selected is not None:
        keys = SELECTED_KEYS | ({"acceptance_criteria"} if selected["how"] in CRITERIA_HOWS else set())
        assert set(selected) == keys
    if named:
        assert NAMED_KEYS <= set(block["named"])
    assert size(block) <= NEXT_BLOCK_CAP
    assert_no_tool_names(serialized(block))


def assert_rows_exact(db, block: dict) -> None:
    """Assert every listed and selected id, revision and state equals the database."""
    items = list(block["open_tasks"])
    if block["selected"] is not None:
        items.append(block["selected"])
    for item in items:
        stored = row(db, item["id"])
        assert (item["revision"], item["state"]) == (stored["revision"], stored["state"])


def assert_no_tool_names(text: str) -> None:
    """Assert result text names no tool: the agent reads fields, not tool advice."""
    from tools.catalog import TOOL_CATALOG

    for name in (*TOOL_CATALOG, "plan_update"):
        assert re.search(rf"\b{re.escape(name)}\b", text) is None, name


def assert_truncated(value: str, stored: str, cap: int) -> None:
    """Assert a longest-prefix cut of `stored` within `cap` serialized bytes plus marker."""
    assert value != stored
    assert value.endswith(MARKER)
    prefix = value[: -len(MARKER)]
    assert stored.startswith(prefix)
    assert size(value) <= cap + QUOTES
    # Longest prefix: one more code point would not have fit under either count.
    one_more = prefix + stored[len(prefix)] + MARKER
    assert size(one_more) > cap


def activate_without_selecting(node_id: str, **kwargs) -> dict:
    """Stand in for an activate that returns without selecting or changing the task."""
    from plans import admission

    return admission.result(node_id, db=kwargs["db"])


def never_select(node_id: str, **_kwargs):
    """Stand in for a selection writer that selects nothing and names no conflict."""
    return None, None


def break_named_start(monkeypatch) -> None:
    """Make every selection path a named start can take return no selection.

    With the slot free and the task eligible, "no selection and no conflict"
    cannot be a reportable outcome, so the close must refuse as an internal
    invariant break.
    """
    from plans import admission

    monkeypatch.setattr(admission, "activate", activate_without_selecting)
    monkeypatch.setattr(admission, "select", never_select)


class EventCapture:
    """Record product events emitted by the plans domain, with commit state at emit."""

    def __init__(self, db) -> None:
        self._db = db
        self.events: list[dict] = []

    def __call__(self, category, action, *args, **kwargs) -> None:
        """Record one emitted event and whether its write had already committed."""
        self.events.append({
            "category": category,
            "action": action,
            "properties": kwargs.get("properties") or {},
            "in_transaction": self._db.in_transaction,
            "receipts": receipt_count(self._db),
        })

    def install(self, monkeypatch) -> "EventCapture":
        """Replace every bound reference to the product-event emitter."""
        import infra.product_events as product_events
        import plans.boundary  # noqa: F401 - bind the boundary before scanning

        original = product_events.emit_product_event
        monkeypatch.setattr(product_events, "emit_product_event", self)
        for name, module in list(sys.modules.items()):
            if name.startswith("plans.") and getattr(module, "emit_product_event", None) is original:
                monkeypatch.setattr(module, "emit_product_event", self)
        return self

    def close_events(self) -> list[dict]:
        """Return the closing-mutation events only."""
        return [
            event for event in self.events
            if (event["category"], event["action"]) == ("plans", "close_next")
        ]
