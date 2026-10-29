"""The reserved ``worker`` namespace: what a rule-started worker returned (design §4.2, §6.1).

Only CORSIA produces these, once per start, on the request's own artifact
(``automation.worker_settle``); intake refuses ``worker.*`` from every other origin.
``worker.result`` carries the checked payload; ``worker.no_result`` says why there is
none (``invalid`` for a payload that failed its workflow's result schema).
"""

from __future__ import annotations

from typing import Any

from events.connector_workflows import shipped_item_fields
from events.connectors import ConnectorManifest, EventTypeDecl, register

NAMESPACE = "worker"
RESULT = "result"
NO_RESULT = "no_result"
#: The fact naming the agent whose rule started the worker: with no artifact owner, the
#: event still reaches that agent's own rules (``events.rules``).
REQUESTER_FACT = "requester"

_FACTS = (
    REQUESTER_FACT, "rule_id", "request_event", "request_identity", "workflow", "source",
    "item", "item_version", "artifact_integration", "artifact_type", "valid", "result",
    "reason", "detail", "kind", "injection_signs",
)


def _classify(raw: dict[str, Any]) -> str:
    return raw.get("kind", "")


def _extract_facts(raw: dict[str, Any], _resolved: Any = None) -> dict[str, Any]:
    return {key: raw[key] for key in ("run_id", *_FACTS, *shipped_item_fields())
            if key in raw and (raw[key] is not None or key == "injection_signs")}


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    """The request's own artifact, so rule 0 reaches its registered owner."""
    keys = ("artifact_integration", "artifact_type", "item")
    if not all(facts.get(key) for key in keys):
        return None
    return {"integration": facts["artifact_integration"], "type": facts["artifact_type"],
            "external_id": str(facts["item"])}


MANIFEST = register(ConnectorManifest(
    connector=NAMESPACE,
    namespace=NAMESPACE,
    version="1",
    event_types={
        RESULT: EventTypeDecl(RESULT, ("run_id",), (*_FACTS, *shipped_item_fields()), event_class="actionable"),
        NO_RESULT: EventTypeDecl(NO_RESULT, ("run_id",), (*_FACTS, *shipped_item_fields()), event_class="informational"),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
    requester_fact=REQUESTER_FACT,
))
