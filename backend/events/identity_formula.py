"""Generic event-identity formula engine (connector-agnostic).

The IdentityFormula is the single dedupe key threaded through intake, delivery,
and automation (AC-5.9). It is derived ONLY from declared normalized facts —
never arrival order, thread state, or a guess (EM2). The same source occurrence
always yields the same ``event_id`` (EM1/EM5); a redelivery is therefore not a
second event.

This module names no connector. Each connector declares *which* facts form its
identity (the ``identity_facts`` tuple on an ``EventTypeDecl``); this engine just
hashes them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


class MalformedEventError(ValueError):
    """An event lacks a fact its identity formula requires (EM4 — fail loud)."""


@dataclass(frozen=True)
class IdentityFormula:
    """Declared: the ordered normalized fact keys that form an event's identity."""

    event_type: str
    fact_keys: tuple[str, ...]


@dataclass(frozen=True)
class RelationshipDecl:
    """A declared parent/child edge between two source object types.

    Concrete edges (a check to its PR, a comment to its document) are declared
    by each connector manifest, never named here — this type is generic.
    """

    from_type: str
    edge: str
    to_type: str


def compute_event_id(
    connector: str,
    event_type: str,
    facts: dict,
    fact_keys: tuple[str, ...],
) -> str:
    """Stable event id from declared facts.

    Raises :class:`MalformedEventError` if any identity fact is missing — an
    event whose identity cannot be formed is malformed and must not register
    (EM4). The id is stable across redelivery because it depends only on the
    declared facts, not on provenance/timestamps.
    """
    missing = [k for k in fact_keys if facts.get(k) is None]
    if missing:
        raise MalformedEventError(
            f"{connector}:{event_type} missing identity fact(s) {missing}; "
            f"have {sorted(facts)}"
        )
    material = "|".join(
        [connector, event_type] + [f"{k}={facts[k]!r}" for k in fact_keys]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{connector}:{event_type}:{digest[:16]}"
