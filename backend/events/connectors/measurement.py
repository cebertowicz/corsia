"""Measurement connector manifest for movement and learning reports.

Both platform-produced report types are informational unless their declared
wake fact promotes them. Their delivery functions name the event type
explicitly, so this connector never infers one report type from the other's
payload. Payload contracts and their strict validators live beside their
delivery functions (``events/movement_report.py``, ``events/learning_report.py``).
"""

from __future__ import annotations

from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register

MEASUREMENT_CONNECTOR = "measurement"
MEASUREMENT_INTEGRATION = "measurement"
MOVEMENT_REPORT_TYPE = "movement-report"
MOVEMENT_DIGEST_EVENT = "measurement.movement_digest"
LEARNING_REPORT_TYPE = "learning-report"
LEARNING_REPORT_EVENT = "measurement.learning_report"
_FILTERABLE = ("scope",)
# The validators admit exact key sets, so a payload carries only its own type's facts.
_FACT_KEYS = ("scope", "report_date", "report_key", "breach", "moves", "wake", "items")


def _classify(raw: dict[str, Any]) -> str:
    return MOVEMENT_DIGEST_EVENT


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    return {k: raw[k] for k in _FACT_KEYS if raw.get(k) is not None}


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    key = facts.get("report_key")
    if key is None:
        return None
    artifact_type = {
        MOVEMENT_DIGEST_EVENT: MOVEMENT_REPORT_TYPE,
        LEARNING_REPORT_EVENT: LEARNING_REPORT_TYPE,
    }[event_type]
    return {"integration": MEASUREMENT_INTEGRATION, "type": artifact_type,
            "external_id": key}


MANIFEST = register(ConnectorManifest(
    connector=MEASUREMENT_CONNECTOR,
    namespace=MEASUREMENT_CONNECTOR,
    version="1",
    event_types={
        MOVEMENT_DIGEST_EVENT: EventTypeDecl(
            MOVEMENT_DIGEST_EVENT, ("report_key",), _FILTERABLE,
            artifact_type=MOVEMENT_REPORT_TYPE,
            artifact_integration=MEASUREMENT_INTEGRATION,
            event_class="informational",
            actionable_when={"breach": True},
            collapse_fact="report_key",
            digest_line_facts=("scope", "report_date", "report_key", "breach", "moves")),
        LEARNING_REPORT_EVENT: EventTypeDecl(
            LEARNING_REPORT_EVENT, ("report_key",), _FILTERABLE,
            artifact_type=LEARNING_REPORT_TYPE,
            artifact_integration=MEASUREMENT_INTEGRATION,
            event_class="informational",
            actionable_when={"wake": True},
            collapse_fact="report_key",
            digest_line_facts=("scope", "report_date", "report_key", "wake")),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
))
