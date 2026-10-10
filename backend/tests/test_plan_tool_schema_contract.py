"""Machine-readable plan tool contracts must be complete on first use."""

from __future__ import annotations

import pytest


_MUTATION_REQUIRED = {
    "add": {"title", "acceptance_criteria"},
    "insert": {"title", "acceptance_criteria"},
    "edit": {"node_id", "expected_revision"},
    "split": {"node_id", "title", "acceptance_criteria", "expected_revision"},
    "reorder": {"node_id", "order_key", "expected_revision"},
    "activate": {"node_id", "expected_revision"},
    "start": {"node_id", "expected_revision"},
    "resume_node": {"node_id", "hold_id", "expected_revision"},
    "block_node": {
        "node_id", "reason_key", "reason", "owner_kind", "expected_revision",
    },
    "resolve_blocker": {"node_id", "hold_id", "expected_revision"},
    "request_review": {"node_id", "evidence", "expected_revision"},
    "mark_done": {"node_id", "evidence", "expected_revision"},
    "reject": {"node_id", "feedback", "expected_revision"},
    "fail": {"node_id", "expected_revision"},
    "attach_spawned_thread": {
        "node_id", "spawned_thread_id", "expected_revision",
    },
    "replace_spawned_thread": {
        "node_id", "spawned_thread_id", "expected_revision",
    },
    "accept": {"node_id", "expected_revision"},
    "accept_with_follow_up": {
        "node_id", "title", "acceptance_criteria", "expected_revision",
    },
    # expected_event, or a reason that schedules the task instead.
    "arm_watch": {"node_id", "condition", "expected_revision"},
    "resolve_watch": {"node_id", "watch_id", "expected_revision"},
    "add_dependency": {"node_id", "depends_on_node_id", "expected_revision"},
    "remove_dependency": {"node_id", "depends_on_node_id", "expected_revision"},
    "add_follow_up": {
        "node_id", "title", "acceptance_criteria", "expected_revision",
    },
    "add_correction": {
        "node_id", "title", "acceptance_criteria", "affected_nodes",
        "expected_revision",
    },
    # Two valid bodies, which one required set cannot express: one node with
    # its revision, or node_ids alone. `plans.boundary_fields` enforces the
    # exclusive choice and `plan_operation_docs` publishes it.
    "archive_node": set(),
    "close_node": {"node_id", "reason", "expected_revision"},
}

_DECISION_REQUIRED = {
    "draft": {
        "source_node_id", "title", "context", "tradeoff", "recommendation",
        "rationale", "options", "other_enabled",
    },
    "update": {"decision_id", "expected_revision"},
    "reorder": {"decision_id", "order_key", "expected_revision"},
    "parent_review": {"decision_id", "expected_revision"},
    "raise": {"decision_id", "expected_revision"},
    "invalidate": {"decision_id", "expected_revision"},
}


def _rules(tool) -> dict[str, dict]:
    schema = tool.contract
    rules = {}
    for rule in schema["allOf"]:
        selector = rule["if"]["properties"]["operation"]
        operations = (
            [selector["const"]]
            if "const" in selector
            else selector["enum"]
        )
        for operation in operations:
            rules[operation] = rule["then"]
    return rules


def _allowed_params(tool, rule: dict) -> dict[str, dict]:
    shared = tool.parameters["properties"]["params"]["properties"]
    allowed = rule["properties"]["params"]["properties"]
    return {field: shared[field] for field in allowed}


def test_plan_update_schema_covers_exact_boundary_vocabulary():
    from plans import boundary_fields
    from tools.plans import PlanUpdateTool

    tool = PlanUpdateTool()
    rules = _rules(tool)

    assert set(rules) == set(boundary_fields.OPERATION_FIELDS) - boundary_fields.HUMAN_OPERATIONS
    assert not set(rules) & boundary_fields.HUMAN_OPERATIONS
    assert set(rules) == set(_MUTATION_REQUIRED)
    for operation, rule in rules.items():
        params = rule["properties"]["params"]
        allowed = set(params["properties"])
        assert params["additionalProperties"] is False
        assert allowed == boundary_fields.OPERATION_FIELDS[operation]
        assert set(params.get("required", ())) == _MUTATION_REQUIRED[operation]
        if operation in {"add", "insert"}:
            assert "expected_revision" not in allowed
        else:
            revision = _allowed_params(tool, rule)["expected_revision"]
            assert "target node" in revision["description"].lower()


def test_identical_mutation_shapes_share_one_conditional_contract():
    from tools.plans import PlanUpdateTool

    schema = PlanUpdateTool().contract
    grouped = [
        rule["if"]["properties"]["operation"].get("enum")
        for rule in schema["allOf"]
    ]

    # Only the user pauses or resumes (pause contract rule 1), so agents are
    # never offered either.
    for human in ("resume", "pause", "pause_node"):
        assert not any(human in (operations or ()) for operations in grouped)
    shared = {frozenset(operations) for operations in grouped if operations}
    # `next_node_id` is accepted only by closing operations, so the node/revision
    # shape splits into the selection pair and the closing pair.
    assert {"activate", "start"} in shared
    # `close_node` shared its body with `pause_node`, which agents no longer
    # see. `archive_node` no longer joins `fail` -- its two shapes give it an
    # empty required set and a `node_ids` field of its own.
    assert not any("close_node" in operations for operations in shared)


def test_plan_decision_schema_covers_exact_operation_vocabulary():
    from plans import decision_fields
    from tools.plans import PlanDecisionTool, _DECISION_OPERATIONS

    tool = PlanDecisionTool()
    rules = _rules(tool)

    assert set(rules) == set(_DECISION_OPERATIONS)
    assert set(rules) == set(decision_fields.OPERATION_FIELDS)
    for operation, rule in rules.items():
        params = rule["properties"]["params"]
        assert params["additionalProperties"] is False
        assert set(params["properties"]) == decision_fields.OPERATION_FIELDS[operation]
        assert set(params["required"]) == _DECISION_REQUIRED[operation]
        if operation != "draft":
            revision = _allowed_params(tool, rule)["expected_revision"]
            assert "decision revision" in revision["description"].lower()


@pytest.mark.parametrize("operation", sorted(_MUTATION_REQUIRED))
def test_plan_update_schema_accepts_operation_shape_and_rejects_cross_fields(operation):
    from tools.plans import PlanUpdateTool

    params = {
        "node_id": "node-1",
        "kind": "task",
        "title": "Do work",
        "description": "Bounded work",
        "acceptance_criteria": "Evidence is recorded",
        "expected_revision": 0,
        "order_key": 0,
        "feedback": "Needs another pass",
        "evidence": "Focused proof passed",
        "artifact_ids": ["artifact-1"],
        "affected_nodes": [{"node_id": "node-2", "expected_revision": 1}],
        "spawned_thread_id": "child-1",
        "verification_result": "passed",
        "condition": {"type": "event", "value": "ci.completed"},
        "expected_event": "ci.completed",
        "watch_id": "watch-1",
        "depends_on_node_id": "node-2",
        "reason": "Waiting for required input",
        "reason_key": "missing_instructions",
        "owner_kind": "user",
        "owner_id": "user-1",
        "hold_id": "hold-1",
        "next_node_id": "node-3",
    }
    tool = PlanUpdateTool()
    rule = _rules(tool)[operation]
    allowed = _allowed_params(tool, rule)
    request_params = {key: value for key, value in params.items() if key in allowed}
    request = {
        "plan_id": "plan-1",
        "mutation_id": f"mutation-{operation}",
        "operation": operation,
        "params": request_params,
    }

    assert PlanUpdateTool().validate_params(request) == []
    cross_field = "expected_revision" if operation in {"add", "insert"} else "parent_id"
    request["params"][cross_field] = 0 if cross_field == "expected_revision" else "parent"
    assert any(
        f"unexpected params.{cross_field}" in error
        for error in PlanUpdateTool().validate_params(request)
    )


#: The operations that close their task, restated independently of production,
#: with the whole parameter vocabulary each accepts. The named next task carries
#: no revision evidence of its own, so `next_node_id` is the one addition.
_CLOSING_FIELDS = {
    "request_review": {"node_id", "evidence", "artifact_ids", "expected_revision", "next_node_id"},
    "mark_done": {
        "node_id", "evidence", "verification_result", "expected_revision", "next_node_id",
    },
    "fail": {"node_id", "evidence", "expected_revision", "next_node_id"},
    "accept": {
        "node_id", "verification_result", "evidence", "expected_revision",
        "next_node_id",
    },
    "accept_with_follow_up": {
        "node_id", "title", "acceptance_criteria", "verification_result",
        "evidence", "expected_revision", "next_node_id",
    },
    # Bulk archive is the one closing body with no single node to describe, so
    # node_ids rides alongside and next_node_id is refused with it.
    "archive_node": {
        "node_id", "node_ids", "expected_revision", "next_node_id",
    },
    "close_node": {
        "node_id", "reason", "expected_revision", "next_node_id",
    },
    "pause_node": {"node_id", "reason", "expected_revision", "next_node_id"},
    "block_node": {
        "node_id", "reason_key", "reason", "owner_kind", "owner_id",
        "condition", "ask_artifact_id", "expected_revision", "next_node_id",
    },
    "arm_watch": {
        "node_id", "condition", "artifact_id", "expected_event", "reason",
        "expected_revision", "next_node_id",
    },
}
_CLOSING_OPERATIONS = set(_CLOSING_FIELDS)
#: The user's own pause parks too, but only over HTTP: the agent schema omits it.
_AGENT_CLOSING_OPERATIONS = _CLOSING_OPERATIONS - {"pause_node"}


def test_exactly_the_closing_and_parking_operations_accept_an_optional_next_node_id():
    from plans import boundary_fields
    from prompts.plan_tools import NEXT_NODE_ID_DESCRIPTION
    from tools.plans import PlanUpdateTool

    tool = PlanUpdateTool()
    rules = _rules(tool)
    exposing = {
        operation for operation, rule in rules.items()
        if "next_node_id" in rule["properties"]["params"]["properties"]
    }

    assert set(boundary_fields.NEXT_OPERATIONS) == _CLOSING_OPERATIONS
    assert exposing == _AGENT_CLOSING_OPERATIONS
    assert {
        operation for operation, fields in boundary_fields.OPERATION_FIELDS.items()
        if "next_node_id" in fields
    } == _CLOSING_OPERATIONS
    for operation in _AGENT_CLOSING_OPERATIONS:
        assert boundary_fields.OPERATION_FIELDS[operation] == _CLOSING_FIELDS[operation]
        assert "next_node_id" not in rules[operation]["properties"]["params"].get(
            "required", []
        )
        assert "next_node_id" not in boundary_fields.OPERATION_REQUIRED_FIELDS[operation]
    field = tool.parameters["properties"]["params"]["properties"]["next_node_id"]
    assert field["type"] == "string"
    assert field["description"] == NEXT_NODE_ID_DESCRIPTION


@pytest.mark.parametrize("operation", sorted(set(_MUTATION_REQUIRED) - _AGENT_CLOSING_OPERATIONS))
def test_the_schema_refuses_next_node_id_on_non_closing_operations(operation):
    from tools.plans import PlanUpdateTool

    request = {
        "plan_id": "plan-1", "mutation_id": f"mutation-{operation}",
        "operation": operation, "params": {"next_node_id": "node-3"},
    }

    assert any(
        "unexpected params.next_node_id" in error
        for error in PlanUpdateTool().validate_params(request)
    )


def test_the_mutation_description_names_the_next_block_and_the_named_start():
    from plans.handoff_brief import RESULT_KEY
    from prompts.plan_tools import CLOSE_NEXT_DESCRIPTION, USER_TASKS_DESCRIPTION
    from tools.plans import PlanUpdateTool

    description = PlanUpdateTool().description

    assert description.endswith(CLOSE_NEXT_DESCRIPTION + " " + USER_TASKS_DESCRIPTION)
    assert "next_node_id" in CLOSE_NEXT_DESCRIPTION
    assert "named" in CLOSE_NEXT_DESCRIPTION
    # The field a close result carries beside next is named as it is emitted,
    # and placed where it is emitted: a sibling of next, never a key inside it.
    assert RESULT_KEY == "selected_description"
    assert f"{RESULT_KEY} beside next" in CLOSE_NEXT_DESCRIPTION


def test_add_defaults_kind_and_activate_advertises_its_requirement():
    from tools.plans import PlanUpdateTool

    tool = PlanUpdateTool()
    add = {
        "plan_id": "plan-1",
        "mutation_id": "add-1",
        "operation": "add",
        "params": {"title": "Default kind", "acceptance_criteria": "Done"},
    }
    activate = {
        "plan_id": "plan-1",
        "mutation_id": "activate-1",
        "operation": "activate",
        "params": {"node_id": "node-1"},
    }

    assert tool.validate_params(add) == []
    assert any(
        "missing required params.expected_revision" in error
        for error in tool.validate_params(activate)
    )


#: Published definition bytes (#1939). At bd4c5d3c the two tools measured
#: 18,569 bytes together, half of it conditional rules no model was shown. Raise
#: a budget only with the reason in the same change. plan_update 8,100 -> 8,300
#: (#2232): fail, reject and close_node now say which one fits replaced or
#: handed-over work, the choice agents got wrong for want of it. Workflow task
#: fields stay within the existing 8,300-byte plan_update contract budget.
#: 8,300 -> 8,350: accept names that active work with evidence settles in one
#: call, and close_node that it is not for done work; a week of production
#: showed 1,287 closes against 1,415 accepts, about half the closes finished work.
#: 8,350 -> 8,600: publish Mark done as the unambiguous agent self-completion
#: verb and distinguish its Done outcome from review handoff and follow-up.
#: 8,600 -> 8,650 (DCOL-FR4.3b): a close result gained one published field,
#: selected_description, and an agent that is never told of it cannot use it.
# 8,650 -> 8,760: `size` adds an enum to add, insert, and edit; retain a narrow 13-byte guard.
# 8,760 -> 8,950: `size` publishes its one rubric at the field, because an agent adding an
# ordinary node otherwise gets no scale and leaves size unset; one copy, not one per operation.
# plan_decision 2,600 -> 3,000 (pick-any): `pick` and the list form of `recommendation` are published at the field, because an agent never told of them raises combination options instead.
_PUBLISHED_BYTES = {"plan_update": 8_950, "plan_decision": 3_000}


def test_the_operation_text_names_each_neighboring_settlement_choice():
    from tools.plans import PlanUpdateTool

    lines = {
        line.split(":", 1)[0].lstrip("- "): line
        for line in PlanUpdateTool().parameters["properties"]["operation"]["description"].splitlines()
    }
    assert "accepts as Done without another reviewer" in lines["mark_done"]
    assert "leaves work awaiting another reviewer" in lines["request_review"]
    assert "verified complete" in lines["accept"]
    assert "new follow-up is required" in lines["accept_with_follow_up"]


def test_the_operation_text_steers_replaced_work_to_close_and_handover_to_follow_up():
    """#2232: an agent choosing fail, reject or close reads which one fits.

    Closed work stays on the plan (#2359), so the text no longer says close
    archives, the reason agents failed replaced steps instead.
    """
    from tools.plans import PlanUpdateTool

    lines = {
        line.split(":", 1)[0].lstrip("- "): line
        for line in PlanUpdateTool().parameters["properties"]["operation"]["description"].splitlines()
    }
    assert "replaced work: close_node" in lines["fail"]
    assert "sent back, not failed" in lines["reject"]
    assert "accept_with_follow_up" in lines["reject"]
    assert "not failed" in lines["close_node"] and "archives" not in lines["close_node"]


@pytest.mark.parametrize("name", sorted(_PUBLISHED_BYTES))
def test_published_schema_keeps_the_contract_on_the_server_and_its_size_bounded(name):
    import json

    from tools.plans import PlanDecisionTool, PlanUpdateTool

    tool = {"plan_update": PlanUpdateTool, "plan_decision": PlanDecisionTool}[name]()
    published = tool.parameters
    operation = published["properties"]["operation"]

    assert "allOf" not in published and tool.contract["allOf"]
    assert {key: value for key, value in tool.contract.items() if key != "allOf"} == published
    for rule in _rules(tool).values():
        for key in rule["properties"]["params"].get("required", ()):
            assert key in operation["description"] or key in {"node_id", "expected_revision"}
    assert len(json.dumps(tool.to_schema())) <= _PUBLISHED_BYTES[name]


def test_arm_watch_schema_reuses_the_defer_condition_contract():
    from registry.attention import DEFER_CONDITION_TYPES
    from tools.plans import PlanUpdateTool

    arm_watch = _allowed_params(
        PlanUpdateTool(), _rules(PlanUpdateTool())["arm_watch"],
    )["condition"]

    assert arm_watch["required"] == ["type", "value"]
    assert arm_watch["properties"]["type"]["enum"] == sorted(DEFER_CONDITION_TYPES)
    description = PlanUpdateTool().description
    assert "'plan_id': 'plan-id'" in description
    assert "'mutation_id': 'arm-watch-1'" in description
    assert "'operation': 'arm_watch'" in description
    assert "'params': {'node_id': 'node-id'" in description
    assert "'expected_revision': 0" in description
    assert "'expected_event': 'external.done'" in description
    assert "'type': 'deadline'" in description




def test_the_shared_validator_owns_the_condition_value_rule():
    """One owner refuses an unstorable value, so every caller is covered.

    The value is stored in a single text column, so an object, array or null
    would reach SQLite unbound and surface as an unhandled binding error. The
    rule lives in ``validate_defer_condition`` rather than in this tool's
    schema because ``automation.watches.arm_watch``, the attention HTTP route
    and ``parent_review`` all reach the same storage through that function; a
    check here would leave them exposed and split one rule across two owners.
    """
    from registry.attention import AttentionError, validate_defer_condition

    for condition_type in ("event", "dependency", "milestone"):
        assert validate_defer_condition(
            {"type": condition_type, "value": "concrete"}
        )["value"] == "concrete"
        for rejected in ({"nested": 1}, ["a"], 5, None, "", "   "):
            with pytest.raises(AttentionError, match="non-empty string"):
                validate_defer_condition(
                    {"type": condition_type, "value": rejected}
                )

    # The deadline branch keeps its own stricter normalization.
    assert validate_defer_condition(
        {"type": "deadline", "value": "2030-01-01T00:00:00+00:00"}
    )["value"].startswith("2030-01-01")


def test_arm_watch_declares_the_condition_value_it_accepts():
    """The schema describes the shape; it does not re-decide validity."""
    from tools.plans import PlanUpdateTool

    condition = _allowed_params(
        PlanUpdateTool(), _rules(PlanUpdateTool())["arm_watch"]
    )["condition"]
    value = condition["properties"]["value"]

    assert value["type"] == "string"
    assert "minLength" not in value, (
        "emptiness is the shared validator's rule, not a second one here"
    )
    assert set(condition["required"]) == {"type", "value"}
