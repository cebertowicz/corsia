"""Artifact Review Profile execution (coordination MVP S5a).

An Artifact Review Profile is an artifact-type/subtype-bound review contract — it
replaces the loose "review skill" prose concept; it is NOT a generic skill path.
A profile runs in FRESH context (artifact snapshot + the profile's own
criteria/templates/reviewer context) and never injects parent conversation
context (v1). It returns structured findings + a recommendation for the parent
to use; it does NOT make the final disposition (that is S5b) and is not an Event
Delivery disposition or user-attention class.

The artifact's type/subtype — the binding key — comes from the Artifacts Registry
(``artifact_identities.type`` + an optional ``metadata.subtype``), the single
stable taxonomy source; profiles invent no parallel taxonomy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from infra.database import get_db
from registry import artifact_record as AR

BLOCKING = "blocking"
ADVISORY = "advisory"


class ProfileExecutionError(RuntimeError):
    """A configured profile could not run (missing artifact/info/profile).

    Raised loudly — a profile that cannot run is never a silent pass/no-op
    (PRG27).
    """


@dataclass(frozen=True)
class Finding:
    """One review observation."""

    code: str
    message: str
    severity: str  # blocking | advisory


@dataclass(frozen=True)
class ReviewResult:
    """Structured profile output for the parent (S5b) to act on."""

    profile_id: str
    artifact_id: str
    artifact_type: str
    recommendation: str            # a parent-disposition outcome (RECOMMENDATION only)
    reasons: tuple[str, ...]
    findings: tuple[Finding, ...]
    blocking_failure: bool         # data for S5b to enforce; not the gate itself
    may_approve_to_user: bool      # constraint data: False on a blocking failure
    parent_conversation_context_injected: bool  # always False (v1)
    fresh_context: dict[str, Any]

    def comments(self) -> list[dict[str, Any]]:
        """Findings as artifact-comment payloads (posted via the existing
        comments API — no parallel comment path is created here)."""
        return [{"body": f"[{f.severity}] {f.message}", "code": f.code}
                for f in self.findings]


@dataclass(frozen=True)
class Profile:
    """An artifact-type/subtype-bound review contract."""

    id: str
    artifact_type: str
    subtype: str | None
    criteria: tuple[str, ...]
    template: str | None
    reviewer_context: str | None
    blocking: bool
    trigger_mode: str              # manual | auto
    evaluator: Callable[[dict, "Profile"], tuple[list[Finding], str, list[str]]]


_PROFILES: dict[str, Profile] = {}


def register_profile(profile: Profile) -> Profile:
    """Register a profile (idempotent by id)."""
    _PROFILES[profile.id] = profile
    return profile


def get_profile(profile_id: str) -> Profile:
    """Return a profile or fail loud."""
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise ProfileExecutionError(
            f"no review profile {profile_id!r} (have {sorted(_PROFILES)})") from exc


def default_profile_for(artifact_type: str, subtype: str | None = None) -> Profile:
    """The default profile bound to an artifact type/subtype, or fail loud."""
    best = None
    for p in _PROFILES.values():
        if p.artifact_type != artifact_type:
            continue
        if p.subtype is not None and p.subtype != subtype:
            continue
        # Prefer a subtype-specific profile over a type-wide one.
        if best is None or (p.subtype is not None and best.subtype is None):
            best = p
    if best is None:
        raise ProfileExecutionError(
            f"no review profile bound to artifact type {artifact_type!r}")
    return best


# ── The default user-raise profile (deterministic checklist; no LLM) ─────────

_USER_RAISE_REQUIRED = ("requested_action", "evidence", "confidence")


def _user_raise_evaluator(
    snapshot: dict, profile: Profile
) -> tuple[list[Finding], str, list[str]]:
    """PRG16 checklist over a user-raise snapshot, evaluated deterministically."""
    findings: list[Finding] = []
    for field_name in _USER_RAISE_REQUIRED:
        if not snapshot.get(field_name):
            findings.append(Finding(
                f"missing_{field_name}",
                f"user raise is missing required field {field_name!r}", BLOCKING))
    if not snapshot.get("urgency"):
        findings.append(Finding(
            "urgency_unstated", "urgency/action state not stated", ADVISORY))
    if findings and any(f.severity == BLOCKING for f in findings):
        return findings, "request_revision", [
            "user raise lacks required fields; return to owner for revision"]
    return findings, "approve_to_user", [
        "user raise has explicit action, evidence, and stated confidence"]


register_profile(Profile(
    id="user-raise-default",
    artifact_type="user-raise",
    subtype=None,
    criteria=("user_input_required", "action_explicit", "urgency_justified",
              "evidence_sufficient", "single_ask", "risk_confidence_stated",
              "clear_clearing_outcomes"),
    template=None,
    reviewer_context="Default user-raise review: only earn user attention when "
                     "input is truly required and the ask is complete.",
    blocking=True,
    trigger_mode="manual",
    evaluator=_user_raise_evaluator,
))


# ── Execution ────────────────────────────────────────────────────────────────

def _resolve_artifact(artifact_id: str, db) -> dict:
    """Resolve an artifact (by id or external_id) to its type + snapshot.

    The registry is the stable type/subtype source. Fails loud if absent (PRG27).
    """
    row = AR.identity_row_for(artifact_id, db=db)
    if row is None:
        raise ProfileExecutionError(
            f"artifact {artifact_id!r} is not registered; cannot review it")
    snapshot = json.loads(row["metadata"]) if row["metadata"] else {}
    subtype = snapshot.get("subtype")
    return {"id": row["id"], "type": row["type"], "subtype": subtype,
            "snapshot": snapshot}


def run_review_profile(
    artifact_id: str, *, profile_id: str | None = None, db=None
) -> ReviewResult:
    """Run an Artifact Review Profile fresh against an artifact.

    Fresh context = artifact snapshot + the profile's criteria/template/reviewer
    context ONLY. Parent conversation context is never injected (v1). Fails loud
    if the artifact is unregistered, the profile is unknown, the profile does not
    bind the artifact's type, or the snapshot lacks the info to review.
    """
    _db = db or get_db()
    try:
        artifact = _resolve_artifact(artifact_id, _db)
        atype, subtype, snapshot = artifact["type"], artifact["subtype"], artifact["snapshot"]

        profile = (get_profile(profile_id) if profile_id
                   else default_profile_for(atype, subtype))
        if profile.artifact_type != atype:
            raise ProfileExecutionError(
                f"profile {profile.id!r} binds {profile.artifact_type!r}, not "
                f"the artifact's type {atype!r}")
        if not snapshot:
            raise ProfileExecutionError(
                f"artifact {artifact_id!r} has no snapshot/content to review")

        fresh_context = {
            "artifact_snapshot": snapshot,
            "criteria": profile.criteria,
            "template": profile.template,
            "reviewer_context": profile.reviewer_context,
            # No parent conversation context (v1) — intentionally absent.
        }
        findings, recommendation, reasons = profile.evaluator(snapshot, profile)
        blocking_failure = profile.blocking and any(
            f.severity == BLOCKING for f in findings)

        return ReviewResult(
            profile_id=profile.id,
            artifact_id=artifact_id,
            artifact_type=atype,
            recommendation=recommendation,
            reasons=tuple(reasons),
            findings=tuple(findings),
            blocking_failure=blocking_failure,
            may_approve_to_user=not blocking_failure,
            parent_conversation_context_injected=False,
            fresh_context=fresh_context,
        )
    finally:
        if not db:
            _db.close()
