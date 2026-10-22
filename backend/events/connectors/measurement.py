"""Measurement connector manifest (declared data).

One platform-produced event type, the daily movement report. The report is
informational (a passive digest note) and becomes actionable only when its
producer states ``breach`` true, so a person-attention or cost series that
crossed its threshold wakes the owner. Each report is its own artifact
(scope + report date), so a rule routes it to the scope's conductor as of
delivery time and a later designation move reaches the new owner. The payload
contract and its validator live in ``events/movement_report.py``.
"""

from __future__ import annotations

from typing import Any

from events.connectors import ConnectorManifest, EventTypeDecl, register

MEASUREMENT_CONNECTOR = "measurement"
MEASUREMENT_INTEGRATION = "measurement"
MOVEMENT_REPORT_TYPE = "movement-report"
MOVEMENT_DIGEST_EVENT = "measurement.movement_digest"

_FILTERABLE = ("scope",)


def _classify(raw: dict[str, Any]) -> str:
    return MOVEMENT_DIGEST_EVENT


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    keys = ("scope", "report_date", "report_key", "breach", "moves")
    return {k: raw[k] for k in keys if raw.get(k) is not None}


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    key = facts.get("report_key")
    if key is None:
        return None
    return {"integration": MEASUREMENT_INTEGRATION, "type": MOVEMENT_REPORT_TYPE,
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
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
))
