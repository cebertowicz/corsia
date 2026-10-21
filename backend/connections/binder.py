"""Connections rule binder (coordination MVP S3).

Ordered rules bind one declared Event-Model filterable fact + value to a
registered destination thread. The binder owns ONLY first-seen owner assignment:
an already-owned artifact routes by ownership (rules are not re-run). A
connector-validated own-instance owner declaration binds before operator rules;
then rule precedence is explicit and first-match-wins. An equal-precedence tie to different
destinations surfaces a binder-repair artifact, never a silent tiebreak. The
binder names no connector — it evaluates declared facts and validates rules
against the connector manifest's live filterable fields.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from events.envelope import EventEnvelope
from infra.database import get_db, write_transaction
from registry import artifact_record as AR
from registry import models

REPAIR_INTEGRATION = "connections"
REPAIR_TYPE = "binder-repair"


class StaleRuleFieldError(ValueError):
    """A rule binds a filterable field the connector no longer declares."""


@dataclass(frozen=True)
class Rule:
    """One declared binding: connector fact == value -> destination thread."""

    id: str
    scope: str
    connector: str
    fact: str
    value: str
    destination_thread_id: str
    precedence: int
    enabled: bool


@dataclass(frozen=True)
class ConflictInfo:
    """An equal-precedence tie across distinct destinations (needs repair)."""

    fact: str
    value: str
    precedence: int
    rule_ids: tuple[str, ...]
    destinations: tuple[str, ...]


@dataclass(frozen=True)
class BindResult:
    """Outcome of binding one envelope to an owner."""

    record: AR.ArtifactRecord | None
    via: str                              # ownership | declaration | rule | repair | unbound
    matched_rule_id: str | None = None
    conflict: ConflictInfo | None = None
    repair_artifact: AR.ArtifactRecord | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binding_rule(rule: Any) -> Rule:
    from events.rules import thread_destination

    condition = rule.match[0]
    return Rule(
        id=rule.id, scope=rule.scope, connector=rule.connector,
        fact=condition['field'], value=condition['value'],
        destination_thread_id=str(thread_destination(rule.to)),
        precedence=rule.precedence, enabled=rule.enabled,
    )


def live_filterable_fields(connector: str) -> set[str]:
    """Union of declared filterable fields across a connector's event types."""
    from events.connectors import get_manifest

    manifest = get_manifest(connector)
    fields: set[str] = set()
    for decl in manifest.event_types.values():
        fields.update(decl.filterable_fields)
    return fields


def validate_rule(rule: Rule) -> None:
    """Reject loudly if a rule binds a field the connector no longer declares."""
    fields = live_filterable_fields(rule.connector)
    if rule.fact not in fields:
        raise StaleRuleFieldError(
            f"rule {rule.id} binds field {rule.fact!r} which connector "
            f"{rule.connector!r} does not declare (declared: {sorted(fields)}). "
            "Repair: point the rule at a declared filterable field or disable it."
        )


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_rule(
    *, connector: str, fact: str, value: str, destination_thread_id: str,
    scope: str = "global", precedence: int = 100, db=None,
) -> Rule:
    """Create a rule. Validates the field against the live manifest (loud)."""
    from events.rules import THREAD_PREFIX

    rule = Rule(str(uuid.uuid4())[:12], scope, connector, fact, str(value),
                destination_thread_id, precedence, True)
    validate_rule(rule)
    with write_transaction(db, scope="create_binding_rule") as _db:
        now = _now()
        _db.execute(
            '''INSERT INTO event_rules
               (id, scope, connector, "match", "to", "on", action, mode,
                precedence, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,'first_seen','deliver','default',?,1,?,?)''',
            (rule.id, scope, connector,
             json.dumps([{'field': fact, 'op': 'eq', 'value': str(value)}]),
             THREAD_PREFIX + destination_thread_id, precedence, now, now),
        )
        return rule


def disable_rule(rule_id: str, *, db=None) -> None:
    """Disable a rule (config, not deletion)."""
    with write_transaction(db, scope="disable_binding_rule") as _db:
        _db.execute(
            "UPDATE event_rules SET enabled=0, updated_at=? WHERE id=? AND \"on\"='first_seen'",
            (_now(), rule_id),
        )


def list_rules(connector: str, *, enabled_only: bool = True, db=None) -> list[Rule]:
    """Rules for a connector, in precedence order (lowest precedence first)."""
    _db = db or get_db()
    try:
        from events.rules import list_rules as event_rules

        rows = event_rules(connector, on='first_seen', enabled_only=enabled_only, db=_db)
        return _resolve_bindings(rows, _db)
    finally:
        if not db:
            _db.close()


def stale_rules(connector: str, *, db=None) -> list[Rule]:
    """Enabled rules whose field is no longer declared — loud repair backlog."""
    fields = live_filterable_fields(connector)
    return [r for r in list_rules(connector, db=db) if r.fact not in fields]


# ── Evaluation + binding ─────────────────────────────────────────────────────

def evaluate(facts: dict[str, Any], connector: str, *, db=None) -> list[Rule]:
    """Enabled rules whose fact==value matches, in precedence order."""
    from events.rules import matching_rules

    _db = db or get_db()
    try:
        rows = matching_rules(facts, connector, on='first_seen', db=_db)
        return _resolve_bindings(rows, _db)
    finally:
        if db is None:
            _db.close()


def _create_repair_artifact(
    _db, connector: str, candidate_ref: dict | None, conflict: ConflictInfo,
    repair_owner: str | None,
) -> AR.ArtifactRecord:
    if repair_owner is None:
        raise ValueError(
            f"equal-precedence rule conflict on {connector}:{conflict.fact}="
            f"{conflict.value} needs a repair owner; refusing to drop it silently")
    sig = f"{connector}:{conflict.fact}={conflict.value}"
    identity = {"integration": REPAIR_INTEGRATION, "type": REPAIR_TYPE,
                "external_id": sig}
    record = AR.register_or_resolve(identity, owner_intent=repair_owner, db=_db)
    models.update_metadata(
        REPAIR_INTEGRATION, REPAIR_TYPE, sig,
        {"conflict_rule_ids": list(conflict.rule_ids),
         "conflict_destinations": list(conflict.destinations),
         "matched_fact": conflict.fact, "matched_value": conflict.value,
         "candidate_artifact": candidate_ref},
        db=_db,
    )
    return record


def _record_provenance(_db, ref: dict, metadata: dict[str, Any]) -> None:
    """Write first-seen binding provenance onto the artifact's metadata."""
    models.update_metadata(
        ref["integration"], ref["type"], ref["external_id"], metadata, db=_db,
    )


def _record_rule_provenance(_db, ref: dict, rule: Rule) -> None:
    _record_provenance(
        _db, ref,
        {"bound_by_rule": rule.id, "matched_fact": rule.fact,
         "matched_value": rule.value},
    )


def _declared_owner(facts: dict[str, Any], *, db=None) -> str | None:
    """Return one connector-validated, currently active declared owner."""
    thread_id = facts.get("declared_owner_thread")
    if not isinstance(thread_id, str):
        return None
    from events.delivery import thread_exists_and_active

    return thread_id if thread_exists_and_active(thread_id, db=db) else None


def bind_owner_for(
    artifact_ref: dict | None, facts: dict[str, Any], connector: str | None, *,
    repair_owner: str | None = None, db=None,
) -> BindResult:
    """Resolve an artifact identity to an owner: ownership, declaration, else rule.

    Existing owned artifacts route by ownership (rules are not re-run). A
    first-seen artifact with a validated owner declaration is owned by the
    declared thread; otherwise it is owned by the winning rule's destination.
    An equal-precedence tie to different destinations creates a binder-repair
    artifact instead of guessing. This is the field-level entry resolve_targets
    calls; ``bind_owner`` is the envelope-shaped wrapper.
    """
    _db = db or get_db()
    try:
        if artifact_ref is None:
            return BindResult(None, via="unbound")
        record = AR.register_or_resolve(artifact_ref, db=_db)

        if record.owner_thread is not None:
            return BindResult(record, via="ownership")
        # Existing-but-ownerless (released) artifacts are not re-owned by rules —
        # re-acquiring ownership is the explicit rehome path (S2 semantics).
        if record.lifecycle != "unregistered":
            return BindResult(record, via="unbound")
        declared_owner = _declared_owner(facts, db=_db)
        if declared_owner is not None:
            bound = AR.register_or_resolve(
                artifact_ref, owner_intent=declared_owner, db=_db,
            )
            if bound.owner_thread == declared_owner:
                _record_provenance(
                    _db, artifact_ref,
                    {"ownership_provenance": "declared_in_pr_body"},
                )
                return BindResult(bound, via="declaration")
        if connector is None:
            return BindResult(record, via="unbound")

        matches = evaluate(facts, connector, db=_db)
        if not matches:
            return BindResult(record, via="unbound")

        min_prec = matches[0].precedence
        top = [r for r in matches if r.precedence == min_prec]
        destinations = sorted({r.destination_thread_id for r in top})
        if len(destinations) > 1:
            conflict = ConflictInfo(
                fact=top[0].fact, value=top[0].value, precedence=min_prec,
                rule_ids=tuple(r.id for r in top), destinations=tuple(destinations))
            repair = _create_repair_artifact(
                _db, connector, artifact_ref, conflict, repair_owner)
            return BindResult(record, via="repair", conflict=conflict,
                              repair_artifact=repair)

        winner = top[0]
        bound = AR.register_or_resolve(
            artifact_ref, owner_intent=winner.destination_thread_id, db=_db)
        _record_rule_provenance(_db, artifact_ref, winner)
        return BindResult(bound, via="rule", matched_rule_id=winner.id)
    finally:
        if not db:
            _db.close()


def bind_owner(envelope: EventEnvelope, *, repair_owner: str | None = None,
               db=None) -> BindResult:
    """Envelope-shaped wrapper over :func:`bind_owner_for`."""
    return bind_owner_for(envelope.artifact_ref, envelope.facts,
                          envelope.connector, repair_owner=repair_owner, db=db)

# ── Rules that follow a designation ──────────────────────────────────────────

DESIGNATION_PREFIX = "designation:"
WORKSPACE_DESIGNATION_SCOPE = DESIGNATION_PREFIX + "workspace"


def ensure_designation_rule(
    *, connector: str, fact: str, value: str, precedence: int = 100,
    designation: str = "workspace", db=None,
) -> Rule | None:
    """Create (once) a rule that routes to a scope coordinator and follows it.

    ``designation`` is ``'workspace'`` (the CORSIA agent) or a project id
    (that project's conductor). The persisted destination is a creation-time
    placeholder; reads resolve the current designation, so a move needs no
    copied owner to keep current and no sweep. Returns None while unassigned.
    """
    rule_scope = DESIGNATION_PREFIX + designation
    with write_transaction(db, scope="ensure_designation_rule") as _db:
        existing = _db.execute(
            '''SELECT * FROM event_rules WHERE scope=? AND connector=?
               AND "on"='first_seen' AND enabled=1
               AND json_extract("match", '$[0].field')=?
               AND json_extract("match", '$[0].value')=?''',
            (rule_scope, connector, fact, str(value)),
        ).fetchone()
        if existing is not None:
            from events.rules import from_row

            return next(iter(_resolve_bindings([from_row(existing)], _db)), None)
        from infra.conductor import get_scope_coordinators

        designated = get_scope_coordinators({designation}, db=_db)[designation]
        if not designated:
            return None
        return create_rule(
            connector=connector, fact=fact, value=value,
            destination_thread_id=designated, scope=rule_scope,
            precedence=precedence, db=_db,
        )


def _resolve_bindings(rows, db) -> list[Rule]:
    """Point designation rules at the current designee, dropping undesignated ones.

    The one designation read for every binder path, batched before the row loop.
    """
    from infra.conductor import get_scope_coordinators

    scopes = {row.scope[len(DESIGNATION_PREFIX):] for row in rows
              if row.scope.startswith(DESIGNATION_PREFIX)}
    designations = get_scope_coordinators(scopes, db=db)
    result = []
    for row in rows:
        rule = _binding_rule(row)
        if rule.scope.startswith(DESIGNATION_PREFIX):
            designation = rule.scope[len(DESIGNATION_PREFIX):]
            destination = designations[designation]
            if not destination:
                continue
            rule = replace(rule, destination_thread_id=destination)
        result.append(rule)
    return result
