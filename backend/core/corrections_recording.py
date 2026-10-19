"""Versioned correction-classification facts and their strict input contract.

This module owns the one reusable definition of an owner-authored message and
all immutable classifier identity.  The job owns scheduling and inference; the
Measurement surface can reuse these facts without reproducing either decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from loguru import logger

CORRECTION_EVENT = "measurement.correction"
MESSAGE_SUBJECT_PREFIX = "message:"
COMMENT_SUBJECT_PREFIX = "doc-comment:"
DECISION_SUBJECT_PREFIX = "plan-decision-answer:"
SUBJECT_PREFIXES = {
    "message": MESSAGE_SUBJECT_PREFIX,
    "doc_comment": COMMENT_SUBJECT_PREFIX,
    "plan_decision_answer": DECISION_SUBJECT_PREFIX,
}
LABEL_CORRECTION = "correction"
LABEL_NOT_CORRECTION = "not_correction"
# A healthy, self-tested classifier failed twice on this one input; Measurement never counts it.
LABEL_UNREADABLE = "unreadable"
LABELS = (LABEL_CORRECTION, LABEL_NOT_CORRECTION, LABEL_UNREADABLE)
FAILURE_REASONS = ("none", "timeout", "rss", "child_error")
SYSTEM_WRAPPER_PREFIXES = (
    "[EVENT:",
    "[DELIVERY WARNING",
    "[PASSIVE CONTEXT DIGEST]",
    "[AGENT-SEND",
    "[CORSIA",
)
OWNER_MESSAGE_METADATA_KEYS = ("sender_kind", "routed_event", "send_idempotency_key")
FACT_KEYS = (
    "source_kind",
    "source_id",
    "thread_id",
    "label",
    "asset_id",
    "asset_version",
    "head",
    "threshold",
    "input_policy_id",
    "truncated",
    "original_tokens",
    "failure_reason",
    "classified_at",
)


@dataclass(frozen=True)
class ClassifierVersion:
    """The immutable local-asset identity that makes one fact comparable.

    The threshold is held at the six decimals it is persisted with, so a version read
    back from storage equals the one that wrote it.
    """

    asset_id: str
    version: str
    head: str
    threshold: float
    input_policy_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "threshold", round(float(self.threshold), 6))


def version_attributes(version: ClassifierVersion) -> dict[str, str]:
    """The one persisted encoding of a version.

    Fact attributes, the pending-candidate and weekly-figure SQL matches, the
    activation record, the activation key and the dedupe key all use it.
    """
    return {
        "asset_id": version.asset_id,
        "asset_version": version.version,
        "head": version.head,
        "threshold": f"{version.threshold:.6f}",
        "input_policy_id": version.input_policy_id,
    }


def decode_version(attributes: Mapping[str, Any]) -> ClassifierVersion:
    """Read a persisted version back; KeyError, TypeError or ValueError when malformed."""
    return ClassifierVersion(
        str(attributes["asset_id"]), str(attributes["asset_version"]), str(attributes["head"]),
        float(attributes["threshold"]), str(attributes["input_policy_id"]),
    )


def version_key(version: ClassifierVersion) -> str:
    """The activation-ledger key for one version."""
    return ":".join(version_attributes(version).values())


def version_match_sql(alias: str, version: ClassifierVersion) -> tuple[str, dict[str, str]]:
    """SQL matching ``alias.attributes`` to one version, with its named parameters."""
    params = {f"version_{key}": value for key, value in version_attributes(version).items()}
    clause = " AND ".join(
        f"json_extract({alias}.attributes,'$.{name[len('version_'):]}')=:{name}" for name in params
    )
    return clause, params


@dataclass(frozen=True)
class CorrectionCandidate:
    """One persisted owner source (message, comment or decision answer) eligible to be checked.

    ``arrived`` marks a source classified as it arrives, not by a later scan.
    For a comment or a decision answer, ``quote`` is the person's words as
    written (``content`` is their masked classifier input) and ``about`` is the
    artifact or decision its triage note names.
    """

    source_kind: str
    source_id: str
    thread_id: str
    project_id: str | None
    content: str
    created_at: str
    context: tuple[tuple[str, str], ...] = ()
    arrived: bool = False
    quote: str | None = None
    about: str | None = None


ACTIVATIONS_SETTING = "corrections_classifier_activations"
ACTIVE_VERSION_SETTING = "corrections_classifier_active_version"


@dataclass(frozen=True)
class CorruptActivation:
    """The activation record exists but cannot be read; never the same as no version."""

    reason: str


def active_corrections_version() -> ClassifierVersion | CorruptActivation | None:
    """The sole version Measurement may read; a filling version never leaks through.

    None means no version has been activated yet. A record that exists but is malformed is
    logged and returned as ``CorruptActivation`` so no caller mistakes it for absence.
    """
    from infra.database import get_db
    from infra.settings import get_setting

    db = get_db()
    try:
        active = get_setting(ACTIVE_VERSION_SETTING, db=db)
        if active is None:
            return None
        key = active["key"]
        activation = (get_setting(ACTIVATIONS_SETTING, db=db) or {})[key]
        version = decode_version(activation["version"])
        if version_key(version) != key:
            raise ValueError("activation key does not match its version")
        return version
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        logger.error("corrections activation record is unreadable: {}", type(error).__name__)
        return CorruptActivation(type(error).__name__)
    finally:
        db.close()


def subject_key(source_kind: str, source_id: str) -> str:
    """The non-colliding ``outcome_events`` subject for one correction source."""
    if source_kind not in SUBJECT_PREFIXES:
        raise ValueError(f"unknown correction source kind: {source_kind}")
    return SUBJECT_PREFIXES[source_kind] + source_id


def dedupe_key(candidate: CorrectionCandidate, version: ClassifierVersion) -> str:
    """Stable idempotency key for one source under one classifier version."""
    payload = json.dumps(
        {"subject_key": subject_key(candidate.source_kind, candidate.source_id), **version_attributes(version)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "correction:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metadata(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_wrapper_or_delivery(
    text: str, metadata: object, delivery_keys: tuple[str, ...] = OWNER_MESSAGE_METADATA_KEYS,
) -> bool:
    """Whether a stored row is a system wrapper or a delivery marked by one of ``delivery_keys``.

    The one rule both owner messages and classifier context use, matched by metadata key.
    """
    parsed = _metadata(metadata)
    return text.startswith(SYSTEM_WRAPPER_PREFIXES) or any(key in parsed for key in delivery_keys)


def is_owner_authored_message(
    *, role: object, content: object, metadata: object,
) -> bool:
    """Return whether one stored row is an owner's authored, non-system message.

    Scope membership is a query responsibility; this reusable predicate owns the
    row-level definition shared by the recording job and the served measure.
    """
    if role != "user" or not isinstance(content, str) or not content.strip():
        return False
    return not is_wrapper_or_delivery(content, metadata)


def owner_message_sql_predicate(alias: str = "m") -> tuple[str, dict[str, str]]:
    """Build the SQL-safe superset filter for ``is_owner_authored_message``.

    The Python predicate remains authoritative after fetching rows. This filter only
    projects its canonical role, wrapper, and metadata constants so irrelevant rows
    cannot starve a bounded pending-candidate scan.
    """
    params = {"owner_message_role": "user"}
    clauses = [f"{alias}.role=:owner_message_role", f"length(trim({alias}.content, ' ' || char(9, 10, 13))) > 0"]
    for index, prefix in enumerate(SYSTEM_WRAPPER_PREFIXES):
        # Exact, case-sensitive prefix test like str.startswith; LIKE folds ASCII case.
        key = f"owner_wrapper_prefix_{index}"
        params[key] = prefix
        clauses.append(f"substr({alias}.content, 1, {len(prefix)}) <> :{key}")
    for index, metadata_key in enumerate(OWNER_MESSAGE_METADATA_KEYS):
        key = f"owner_metadata_path_{index}"
        params[key] = f"$.{metadata_key}"
        clauses.append(
            f"json_type(CASE WHEN json_valid({alias}.metadata) THEN {alias}.metadata ELSE '{{}}' END, :{key}) IS NULL"
        )
    return " AND ".join(clauses), params


def fact_attributes(
    candidate: CorrectionCandidate,
    classification: Mapping[str, Any],
    version: ClassifierVersion,
    *,
    classified_at: str | None = None,
) -> dict[str, Any]:
    """Shape one local-classifier fact without retaining message content.

    ``failure_reason`` is ``none`` on a scored fact and names the failure on an
    ``unreadable`` one.
    """
    label, reason = str(classification["label"]), str(classification["failure_reason"])
    if label not in LABELS or reason not in FAILURE_REASONS or (label == LABEL_UNREADABLE) == (reason == "none"):
        raise ValueError(f"inconsistent correction fact: label {label}, failure {reason}")
    return {
        "source_kind": candidate.source_kind,
        "source_id": candidate.source_id,
        "thread_id": candidate.thread_id,
        "label": label,
        **version_attributes(version),
        "truncated": "true" if classification["truncated"] else "false",
        "original_tokens": int(classification["original_tokens"]),
        "failure_reason": reason,
        "classified_at": classified_at or datetime.now(timezone.utc).isoformat(),
    }
