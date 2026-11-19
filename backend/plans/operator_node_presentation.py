"""Present one operator node and its attached artifacts."""

from __future__ import annotations

from plans import operator_plan_display as plan_header, user_steering


def _artifact_fields(link: dict) -> dict:
    return {
        "id": link["artifact_id"],
        "role": link["role"],
        "integration": link["integration"],
        "type": link["type"],
        "external_id": link["external_id"],
        "title": link.get("title"),
        "path": link.get("path"),
        "state": link["state"],
        "attention_state": link.get("attention_state"),
    }


def present_node(node: dict, review_targets: dict[str, dict], state: dict, plan_state: str) -> dict:
    """Adapt a node and its canonical state into the operator row."""
    links = node["artifact_links"]
    discussion = next(
        (link["artifact_id"] for link in links if link["role"] == "discussion"),
        None,
    )
    completion = None
    if node["completed_event_id"]:
        completion = {
            "event_id": node["completed_event_id"],
            "at": node["completed_at"],
            "witnessed": False,
        }
    execution = state["execution"]
    # Precedence decides disclosure: only a primarily blocked node opens its
    # ancestor path. A paused, review, or terminal node keeps its blocker as a
    # detail cause without widening what the tree opens by default.
    attention_path = (
        [*node["path"], node["id"]] if execution["condition"] == "blocked" else []
    )
    return {
        "id": node["id"],
        "parent_id": node["parent_id"],
        "depth": node["depth"],
        "kind": node["kind"],
        "title": node["title"],
        "description": node["description"],
        # A person step names only this plan's open dependents; cross-plan
        # dependents remain private to their plan owner.
        "waiting_on": node["dependent_titles"] if node["assignee"] == "user" else [],
        "acceptance_criteria": node["acceptance_criteria"],
        "state": node["state"],
        "unmet_dependencies": list(node["unmet_dependencies"]),
        "held_by": node["held_by"],
        "assignee": node["assignee"],
        "revision": node["revision"],
        "requirements_revision": node["requirements_revision"],
        "commitment_id": node["commitment_id"],
        "updated_at": node["updated_at"],
        "last_transition_at": state["last_transition_at"],
        "requires_user_action": state["requires_user_action"],
        "executor": state["executor"],
        "execution": execution,
        "display": state["display"],
        "status_reason": state["status_reason"],
        "evidence": state["evidence"],
        "commitment_cost": state["commitment_cost"],
        "node_cost": state["node_cost"],
        "initiative": state["initiative"],
        "branch_progress": None,
        "scheduled_until": None,
        "attention_path": attention_path,
        "completion": completion,
        "child": plan_header.child_summary(node["child_plan"], node["spawned_thread_id"]),
        "review": review_targets.get(node["id"]),
        "user_actions": user_steering.offered(
            node, review_targets.get(node["id"]), execution["causes"],
            plan_state=plan_state,
            worker_plan_state=(
                node["child_plan"].plan["state"] if node["child_plan"] else None
            ),
        ),
        "discussion_artifact_id": discussion,
        "artifacts": [
            _artifact_fields(link) for link in links if link["role"] != "discussion"
        ],
    }
