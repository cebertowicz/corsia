"""The R14 projection for plan content crossing a project fence."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from infra.project_fence import ALL, WITHHELD_LOCAL_ONLY, fenced_project_ids, read_caller

# These keys carry plan vocabulary rather than free-form text. Everything else
# is withheld unless it is an identifier or a timestamp.
_STATES = frozenset({
    "pending", "active", "paused", "review_requested", "accepted", "accepted_with_followup",
    "failed", "closed", "armed", "resolved", "expired", "draft", "raised", "parent_reviewed",
})
_ENUM_VALUES = {
    **{key: _STATES for key in ("state", "prior_state", "new_state", "status")},
    "kind": frozenset({"phase", "track", "task", "paused", "blocked", "waiting", "thread", "project_scope", "workspace_scope", "user", "external", "system", "human_review", "human_decision", "parent_agent"}),
    "owner_kind": frozenset({"thread", "project_scope", "workspace_scope", "user", "external", "system"}),
    "condition": frozenset({"running", "waiting", "blocked", "paused", "ready", "review"}),
    "condition_type": frozenset({"deadline", "dependency", "event", "milestone"}),
    "type": frozenset({"deadline", "dependency", "event", "milestone"}),
    "label_key": frozenset({"working", "needs_you", "needs_agent_input", "waiting", "finished"}),
    "tone_key": frozenset({"active", "human-review", "waiting", "complete", "failed", "closed"}),
    "liveness": frozenset({"idle", "advancing", "waiting", "blocked", "paused"}),
    "assignee": frozenset({"user", "coordinator", "subagent"}),
    "size": frozenset({"XS", "S", "M", "L", "XL", "XXL", "unknown"}),
    "section": frozenset({"current", "recent"}),
    "severity": frozenset({"idle", "moving", "waiting", "blocked"}),
    "spawn_kind": frozenset({"delegated", "claude-code", "codex", "native"}),
}


def _timestamp(key: str, value: str) -> bool:
    if not (key.endswith("_at") or key in {"as_of", "at", "deadline", "scheduled_until", "until", "value"}):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def project(value: Any, key: str = "") -> Any:
    """Keep structural facts; unknown string fields are withheld by default."""
    if isinstance(value, dict):
        return {k: project(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [project(item, key) for item in value]
    if isinstance(value, str):
        if key == "id" or key.endswith(("_id", "_ids")) or value in _ENUM_VALUES.get(key, ()) or _timestamp(key, value):
            return value
        return WITHHELD_LOCAL_ONLY
    return value


def fences_thread(db, recipient: str | None, owner: str | None) -> bool:
    """Whether recipient is fenced from the project currently owning the text."""
    if owner is None:
        return False
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (owner,)).fetchone()
    if row is None or row["project_id"] is None:
        return False
    fenced = fenced_project_ids(read_caller(recipient, db=db), db=db)
    return fenced is ALL or (isinstance(fenced, frozenset) and str(row["project_id"]) in fenced)


def for_thread(value: Any, *, recipient: str | None, owner: str | None, db) -> Any:
    """Project plan content if its owner is across the recipient's fence."""
    return project(value) if fences_thread(db, recipient, owner) else value


def dependencies(facts: dict, *, recipient: str | None, plan_owner: str | None, db) -> None:
    """Withhold foreign prerequisite text inside an otherwise visible plan."""
    if fences_thread(db, recipient, plan_owner):
        return  # The complete plan is projected at the read boundary.
    for node in facts["nodes"]:
        for cause in node.get("execution", {}).get("causes", []):
            if cause.get("reason_key") not in {"dependency", "dependency_failed"}:
                continue
            reason = cause.get("reason")
            if reason and fences_thread(db, recipient, cause.get("owner_id")):
                cause["reason"] = project(reason)
                if node.get("status_reason") == reason:
                    node["status_reason"] = project(reason)
                if node["execution"].get("primary_reason") == reason:
                    node["execution"]["primary_reason"] = project(reason)


def admission_result(result: dict, *, recipient: str, plan_id: str, db) -> None:
    """Project foreign dependency reasons in a mutation's admission result."""
    execution = result.get("admission")
    if not isinstance(execution, dict):
        return
    row = db.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    owner = row["owner_id"] if row is not None and row["owner_kind"] == "thread" else None
    dependencies({"nodes": [{"execution": execution}]}, recipient=recipient, plan_owner=owner, db=db)


def wake_next(block: dict, *, recipient: str, plan_id: str, db) -> dict:
    """Remove fenced prerequisite reasons from the recipient's next-work block."""
    from plans import admission

    row = db.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is not None and row["owner_kind"] == "thread" and fences_thread(db, recipient, row["owner_id"]):
        return block  # The wake writer projects the entire source plan.
    for item in block.get("open_tasks", []):
        node_id = item.get("id")
        if not node_id or not item.get("wait"):
            continue
        execution = admission.project(node_id, db=db)
        for cause in execution.get("causes", []):
            if (cause.get("reason_key") in {"dependency", "dependency_failed"}
                    and cause.get("reason") == execution.get("primary_reason")
                    and fences_thread(db, recipient, cause.get("owner_id"))):
                item["wait"] = project(item["wait"])
                break
    return block


def notice(summary: str, details: dict, *, parent_plan_id: str, db) -> tuple[str, dict]:
    """Project a worker notice at its receiving parent's boundary."""
    parent = db.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (parent_plan_id,)).fetchone()
    worker = details.get("worker_thread_id")
    if parent is None or parent["owner_kind"] != "thread" or not worker:
        return summary, details
    if fences_thread(db, str(parent["owner_id"]), worker):
        return project(summary), project(details)
    return summary, details


def for_plan(value: Any, *, recipient: str | None, plan_id: str | None, db, node_id: str | None = None) -> Any:
    """Project content from a plan for its wake recipient."""
    if node_id is not None:
        node = db.execute("SELECT plan_id FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
        if node is not None:
            plan_id = node["plan_id"]
    if plan_id is None:
        return value
    row = db.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None or row["owner_kind"] != "thread":
        return value
    return for_thread(value, recipient=recipient, owner=row["owner_id"], db=db)


def wake_content(content: str, *, recipient: str, plan_id: str, db) -> str:
    """Project a wake's prose and structured JSON tail, retaining actionable IDs."""
    row = db.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None or row["owner_kind"] != "thread" or not fences_thread(db, recipient, row["owner_id"]):
        return content
    lines = content.splitlines()
    try:
        tail = json.loads(lines[-1])
    except (IndexError, ValueError):
        return project(content)
    if not isinstance(tail, dict):
        return project(content)
    return f"{project('text')}\n{json.dumps(project(tail), sort_keys=True)}"
