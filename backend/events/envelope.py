"""The canonical event envelope + the single normalization entrypoint.

``normalize`` turns any source occurrence into one ``EventEnvelope`` before
anything routes, wakes, or automates (EM13). It names NO connector: it looks up
the source's manifest (declared data) and applies the declared formula,
fact-extraction, relationships, and artifact identity. Connector differences are
data; this code is the same for every source (ED22/EM38) — the no-bespoke-router
guard keeps it that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from events.connectors import EventTypeDecl, get_manifest
from events.identity_formula import compute_event_id


@dataclass(frozen=True)
class EventEnvelope:
    """One normalized event. ``event_id`` is the single dedupe key (AC-5.9)."""

    event_id: str
    type: str
    connector: str | None
    facts: dict[str, Any]
    relationships: tuple[dict[str, str], ...]
    artifact_ref: dict[str, str] | None
    provenance: dict[str, Any]
    event_class: str
    ownerless_ok: bool
    origin: str = "platform"


def _event_class(decl: EventTypeDecl, facts: dict[str, Any]) -> str:
    """The type's class, promoted to actionable only by its declared fact rule."""
    rule = decl.actionable_when
    if rule and all(k in facts and facts[k] == v for k, v in rule.items()):
        return "actionable"
    return decl.event_class


def normalize(
    connector: str, raw_payload: dict[str, Any], event_type: str | None = None
) -> EventEnvelope:
    """Normalize a raw source payload into a canonical envelope.

    Rejects loudly (``UnregisteredConnectorError`` / ``UnregisteredEventTypeError``
    / ``MalformedEventError``) rather than guessing — an event whose source,
    type, or identity facts are unknown never registers (EM7/EM4/EM19).
    """
    manifest = get_manifest(connector)
    etype = event_type or manifest.classify(raw_payload)
    decl = manifest.decl(etype)

    facts = manifest.extract_facts(raw_payload)
    event_id = compute_event_id(connector, etype, facts, decl.identity_facts)
    artifact_ref = manifest.build_artifact_ref(etype, facts, raw_payload)
    relationships = manifest.resolve_relationships(etype, facts, raw_payload)

    provenance = {
        "source": manifest.namespace,
        "conn_version": manifest.version,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "rule_trace": [],
    }
    return EventEnvelope(
        event_id=event_id,
        type=etype,
        connector=connector,
        facts=facts,
        relationships=tuple(relationships),
        artifact_ref=artifact_ref,
        provenance=provenance,
        event_class=_event_class(decl, facts),
        ownerless_ok=decl.ownerless_ok,
    )
