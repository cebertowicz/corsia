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

CORRECTION_EVENT = "measurement.correction"
MESSAGE_SUBJECT_PREFIX = "message:"
LABEL_CORRECTION = "correction"
LABEL_NOT_CORRECTION = "not_correction"
LABELS = (LABEL_CORRECTION, LABEL_NOT_CORRECTION)
SYSTEM_WRAPPER_PREFIXES = (
    "[EVENT:",
    "[DELIVERY WARNING",
    "[PASSIVE CONTEXT DIGEST]",
    "[AGENT-SEND",
    "[CORSIA",
)
OWNER_MESSAGE_METADATA_KEYS = ("sender_kind", "routed_event", "send_idempotency_key")
FACT_KEYS = (
    "message_id",
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
class CheckerVersion:
    """The immutable local-asset identity that makes one fact comparable."""

    asset_id: str
    version: str
    head: str
    threshold: float
    input_policy_id: str


@dataclass(frozen=True)
class CorrectionCandidate:
    """One persisted user message eligible to be checked."""

    message_id: str
    thread_id: str
    project_id: str | None
    content: str
    created_at: str
    context: tuple[tuple[str, str], ...] = ()


ACTIVATIONS_SETTING = "corrections_classifier_activations"
ACTIVE_VERSION_SETTING = "corrections_classifier_active_version"


def active_corrections_version() -> CheckerVersion | None:
    """The sole version Measurement may read; a filling version never leaks through."""
    from infra.database import get_db
    from infra.settings import get_setting

    db = get_db()
    try:
        active = get_setting(ACTIVE_VERSION_SETTING, db=db) or {}
        key = active.get("key")
        activations = get_setting(ACTIVATIONS_SETTING, db=db) or {}
        activation = activations.get(key) if isinstance(key, str) else None
        saved = activation.get("version") if isinstance(activation, dict) else None
        if not isinstance(saved, dict):
            return None
        return CheckerVersion(
            str(saved["asset_id"]), str(saved["version"]), str(saved["head"]),
            float(saved["threshold"]), str(saved["input_policy_id"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    finally:
        db.close()


def subject_key(message_id: str) -> str:
    """The ``outcome_events`` subject for one durable message."""
    return MESSAGE_SUBJECT_PREFIX + message_id


def dedupe_key(message_id: str, version: CheckerVersion) -> str:
    """Stable idempotency key for one message under one checker version."""
    payload = json.dumps(
        {"message_id": message_id, "asset_id": version.asset_id,
         "version": version.version, "head": version.head,
         "threshold": version.threshold, "input_policy_id": version.input_policy_id},
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


def is_owner_authored_message(
    *, role: object, content: object, metadata: object,
) -> bool:
    """Return whether one stored row is an owner's authored, non-system message.

    Scope membership is a query responsibility; this reusable predicate owns the
    row-level definition shared by the recording job and the served measure.
    """
    if role != "user" or not isinstance(content, str) or not content.strip():
        return False
    parsed = _metadata(metadata)
    if any(key in parsed for key in OWNER_MESSAGE_METADATA_KEYS):
        return False
    return not content.startswith(SYSTEM_WRAPPER_PREFIXES)


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
    version: CheckerVersion,
    *,
    classified_at: str | None = None,
) -> dict[str, Any]:
    """Shape one local-classifier fact without retaining message content."""
    return {
        "message_id": candidate.message_id,
        "thread_id": candidate.thread_id,
        "label": classification["label"],
        "asset_id": version.asset_id,
        "asset_version": version.version,
        "head": version.head,
        "threshold": f"{version.threshold:.6f}",
        "input_policy_id": version.input_policy_id,
        "truncated": "true" if classification["truncated"] else "false",
        "original_tokens": int(classification["original_tokens"]),
        "failure_reason": str(classification.get("failure_reason", "none")),
        "classified_at": classified_at or datetime.now(timezone.utc).isoformat(),
    }
