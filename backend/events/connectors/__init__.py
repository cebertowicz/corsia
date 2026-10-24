"""Connector manifests — the declared-data home for source-specific facts.

A connector contributes CORSIA routability as **data, not code** (PS32): each
manifest declares its event types, the facts they carry, the identity formula,
relationships, artifact identity, and artifact-producing command triggers. The
generic event and result-observer paths read these manifests; they never branch
on a connector name. This is the only place connector tokens may
appear (enforced by ``tests/coordination_mvp/test_guards.py``).

Adding a new integration = register a new manifest here. No release.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from events.identity_formula import RelationshipDecl


@dataclass(frozen=True)
class EventTypeDecl:
    """A normalized (product/domain) event type a connector can emit."""

    event_type: str                                   # e.g. "pr.opened"
    identity_facts: tuple[str, ...]                    # IdentityFormula input keys
    filterable_fields: tuple[str, ...]                 # facts user/agent rules may bind
    artifact_type: str | None = None                   # registry type for artifact_ref
    artifact_integration: str | None = None            # registry integration
    relationships: tuple[RelationshipDecl, ...] = ()
    event_class: str = "informational"                 # actionable | informational
    ownerless_ok: bool = False
    # Declared fact rule (every field must equal) that promotes one event of an
    # informational type to actionable. A producer never sets the class; a
    # missing fact fails closed to ``event_class``.
    actionable_when: Mapping[str, Any] | None = None
    # Fact whose value groups this type's digest notes (one note per value).
    collapse_fact: str | None = None
    # Facts a digest note's line shows the agent, in this order. Never a fact
    # carrying outside text (a subject, a sender): undeclared facts stay out.
    digest_line_facts: tuple[str, ...] = ()
    # The name the platform's own router gives this event (a webhook's internal
    # event), when one routes it; agent-written rules on that path read this type.
    routed_event: str | None = None


@dataclass(frozen=True)
class ObservedArtifactDecl:
    """Canonical artifact identity extracted from a confirmed command result."""

    integration: str
    type: str
    external_id: str
    title: str | None = None
    path: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CommandTriggerDecl:
    """One direct command a connector can observe for artifact ownership."""

    name: str
    tool_name: str
    argv_prefix: tuple[str, ...]
    extract_artifact: Callable[[str], ObservedArtifactDecl | None]
    command_arg: str = "command"


@dataclass(frozen=True)
class ConnectorManifest:
    """All declared data + resolvers for one source connector / internal domain."""

    connector: str                                     # "github" | "gmail" | "document" | "plan"
    namespace: str                                     # source namespace (EM35)
    version: str
    event_types: Mapping[str, EventTypeDecl]
    classify: Callable[[dict], str]                    # raw payload -> event_type
    # raw payload + what routing already resolved for it (or None) -> flat facts
    extract_facts: Callable[[dict, Mapping[str, Any] | None], dict]
    build_artifact_ref: Callable[[str, dict, dict], dict | None]
    command_triggers: tuple[CommandTriggerDecl, ...] = ()
    resolve_relationships: Callable[[str, dict, dict], tuple[dict, ...]] = (
        lambda event_type, facts, raw: ()
    )
    # The fact naming the repository an event comes from, the repositories a
    # project tracks, and its inverse, the projects tracking one repository
    # (``None`` when they can't be read). Declared together: a rule's test on
    # that fact must name one the writer's project tracks, and an event routing
    # gave no thread reaches the self-rules of the projects tracking its repository.
    repository_fact: str | None = None
    project_repositories: Callable[[str], frozenset[str] | None] | None = None
    repository_projects: Callable[[str], frozenset[str] | None] | None = None
    # A CORSIA-produced (platform) event's fact naming the agent that asked for it: with no
    # artifact owner, the event still reaches that agent's own rules (``events.rules``).
    requester_fact: str | None = None

    def routed_type(self, routed_event: str) -> str | None:
        """The declared type the router's ``routed_event`` is, if one declares it."""
        return next((decl.event_type for decl in self.event_types.values()
                     if decl.routed_event == routed_event), None)

    def decl(self, event_type: str) -> EventTypeDecl:
        """Return a declared event type or reject loudly (EM7)."""
        try:
            return self.event_types[event_type]
        except KeyError as exc:
            raise UnregisteredEventTypeError(
                f"{self.connector}: event type {event_type!r} not declared "
                f"(have {sorted(self.event_types)})"
            ) from exc


class UnregisteredEventTypeError(KeyError):
    """An event type that no manifest declares — rejected before delivery (EM7)."""


class UnregisteredConnectorError(KeyError):
    """A source with no registered manifest — rejected loudly (EM11)."""


_REGISTRY: dict[str, ConnectorManifest] = {}


def register(manifest: ConnectorManifest) -> ConnectorManifest:
    """Register a connector manifest (idempotent by connector name)."""
    _REGISTRY[manifest.connector] = manifest
    return manifest


def get_manifest(connector: str) -> ConnectorManifest:
    """Return a registered manifest or reject loudly (EM11)."""
    try:
        return _REGISTRY[connector]
    except KeyError as exc:
        raise UnregisteredConnectorError(
            f"no manifest registered for connector {connector!r} "
            f"(have {sorted(_REGISTRY)})"
        ) from exc


def registered_connectors() -> tuple[str, ...]:
    """All registered connector names."""
    return tuple(sorted(_REGISTRY))


def registered_command_triggers(
    tool_name: str,
) -> tuple[tuple[str, CommandTriggerDecl], ...]:
    """Return connector-declared command triggers for one canonical tool."""
    return tuple(
        (connector, trigger)
        for connector in sorted(_REGISTRY)
        for trigger in _REGISTRY[connector].command_triggers
        if trigger.tool_name == tool_name
    )


def _load_builtin_manifests() -> None:
    """Import the built-in connector modules so they self-register."""
    from events.connectors import (  # noqa: F401
        document,
        github,
        gmail,
        library,
        mailhost,
        measurement,
        microsoft,
        plan,
        worker,
    )


_load_builtin_manifests()
