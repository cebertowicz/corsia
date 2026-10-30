"""Internal structured-plan material-event manifest (declared data)."""

from __future__ import annotations

from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register

PLAN_INTEGRATION = "local"
PLAN_TYPE = "plan"

#: A block and a review wake the parent; a completion is a digest fact.
_MATERIAL_TYPES = {
    "plan.node_blocked": "actionable",
    "plan.review_requested": "actionable",
    "plan.node_completed": "informational",
}
_FILTERABLE = ("plan_id", "node_id", "plan_revision")


def _classify(raw: dict[str, Any]) -> str:
    return raw.get("material_event", "")


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    keys = ("plan_id", "node_id", "plan_event_id", "plan_revision")
    return {k: raw[k] for k in keys if raw.get(k) is not None}


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    plan_id = facts.get("plan_id")
    if plan_id is None:
        return None
    return {"integration": PLAN_INTEGRATION, "type": PLAN_TYPE, "external_id": plan_id}


MANIFEST = register(ConnectorManifest(
    connector="plan",
    namespace="plan",
    version="2",
    event_types={event_type: EventTypeDecl(
        event_type, ("node_id", "plan_event_id"), _FILTERABLE,
        artifact_type=PLAN_TYPE, artifact_integration=PLAN_INTEGRATION,
        event_class=event_class,
    ) for event_type, event_class in _MATERIAL_TYPES.items()},
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
))
