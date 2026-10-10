"""Durable host-access lease state, identity, and lineage.

This module is the sole owner of lease application normalization, hashing,
transactions, state transitions, expiry, and public row shaping. Runtime
reviewers and execution gates consume it; they do not recreate lease truth.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import re
import sqlite3
from typing import Any, Final, Iterator, Mapping
from uuid import uuid4

from host_access.redaction import (
    CommandRedactionError,
    validate_action_descriptor,
)
from host_access.review_records import encode_application_review
from infra.database import get_db, write_transaction
from infra.host_access_exceptions import (
    EXTERNAL_AGENT_POLICY,
    normalize_requested_policy_exceptions,
)
from infra.project_repo_policy import (
    load_project_repo_policy_snapshot,
    RepoPolicyQueryError,
)
from infra.time import utc_now


NONTERMINAL_STATES = frozenset(
    {"reviewing", "awaiting_user", "activating", "active"}
)
TERMINAL_STATES = frozenset({"expired", "revoked", "blocked", "rejected"})
_APPLICATION_VERSION = 3
MAX_LEASE_DURATION_SECONDS: Final[int] = 30 * 60
MAX_PURPOSE_CHARS = 2000
MAX_PLAN_STEPS = 20
MAX_PLAN_STEP_CHARS = 1000
MAX_RISK_FIELD_CHARS = 2000
MAX_POLICY_EXCEPTIONS = 8
MAX_POLICY_EXCEPTION_REASON_CHARS = 1000
MAX_POLICY_EXCEPTION_REPO_NAME_CHARS = 255
POLICY_EXCEPTION_KINDS = (
    "branch_pinning",
    "protected_base_checkout",
)
MAX_REVIEWER_MODEL_CHARS = 500
MAX_TERMINAL_REASON_CHARS = 2000
MAX_TOOL_NAME_CHARS = 200
MAX_ACTION_DESCRIPTOR_CHARS = 16_000
ACTION_PERMIT_TTL_SECONDS = 10


class LeaseError(RuntimeError):
    """Base class for durable lease failures."""


class LeaseValidationError(LeaseError):
    """The submitted application is not reviewable."""


class LeaseConflictError(LeaseError):
    """A state, revision, hash, or concurrent application conflicts."""


class LeaseNotFoundError(LeaseError):
    """The lease does not exist for the authoritative user."""


class LeaseLineageError(LeaseError):
    """Canonical spawned-by lineage cannot be proven safely."""


def _text(
    value: Any,
    field: str,
    *,
    required: bool,
    max_chars: int,
) -> str | None:
    if value is None:
        if required:
            raise LeaseValidationError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise LeaseValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        if required:
            raise LeaseValidationError(f"{field} must not be blank")
        return None
    if len(normalized) > max_chars:
        raise LeaseValidationError(
            f"{field} must not exceed {max_chars} characters"
        )
    return normalized


def _normalize_application(
    *,
    purpose: Any,
    plan: Any,
    duration_seconds: Any,
    expected_resources: Any = None,
    persistent_effects: Any = None,
    rollback_plan: Any = None,
    forbidden_effects: Any = None,
    requested_policy_exceptions: Any = None,
) -> dict[str, Any]:
    if not isinstance(plan, list) or not plan:
        raise LeaseValidationError("plan must be a nonempty ordered array")
    if len(plan) > MAX_PLAN_STEPS:
        raise LeaseValidationError(
            f"plan must not exceed {MAX_PLAN_STEPS} steps"
        )
    normalized_plan = [
        _text(
            step,
            "plan step",
            required=True,
            max_chars=MAX_PLAN_STEP_CHARS,
        )
        for step in plan
    ]
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise LeaseValidationError("duration_seconds must be an integer")
    if not 1 <= duration_seconds <= MAX_LEASE_DURATION_SECONDS:
        raise LeaseValidationError(
            "duration_seconds must be from 1 through "
            f"{MAX_LEASE_DURATION_SECONDS}"
        )
    normalized_exceptions = normalize_requested_policy_exceptions(
        requested_policy_exceptions,
        max_exceptions=MAX_POLICY_EXCEPTIONS,
        max_repo_name_chars=MAX_POLICY_EXCEPTION_REPO_NAME_CHARS,
        max_reason_chars=MAX_POLICY_EXCEPTION_REASON_CHARS,
        max_command_chars=MAX_ACTION_DESCRIPTOR_CHARS,
        repo_policy_kinds=POLICY_EXCEPTION_KINDS,
        validation_error=LeaseValidationError,
    )
    return {
        "purpose": _text(
            purpose,
            "purpose",
            required=True,
            max_chars=MAX_PURPOSE_CHARS,
        ),
        "plan": normalized_plan,
        "duration_seconds": duration_seconds,
        "expected_resources": _text(
            expected_resources,
            "expected_resources",
            required=False,
            max_chars=MAX_RISK_FIELD_CHARS,
        ),
        "persistent_effects": _text(
            persistent_effects,
            "persistent_effects",
            required=False,
            max_chars=MAX_RISK_FIELD_CHARS,
        ),
        "rollback_plan": _text(
            rollback_plan,
            "rollback_plan",
            required=False,
            max_chars=MAX_RISK_FIELD_CHARS,
        ),
        "forbidden_effects": _text(
            forbidden_effects,
            "forbidden_effects",
            required=False,
            max_chars=MAX_RISK_FIELD_CHARS,
        ),
        "requested_policy_exceptions": normalized_exceptions,
    }


def _validate_policy_exceptions(
    exceptions: list[dict[str, str]],
    *,
    project_id: str | None,
    repo_policy_snapshot: list[dict[str, Any]],
) -> None:
    if not exceptions:
        return
    repo_exceptions = [
        exception
        for exception in exceptions
        if exception.get("policy") != EXTERNAL_AGENT_POLICY
    ]
    if not repo_exceptions:
        return
    if project_id is None:
        raise LeaseValidationError(
            "projectless host-access requests cannot request repository policy exceptions"
        )
    active: set[tuple[str, str]] = set()
    for repo in repo_policy_snapshot:
        repo_name = repo["repo_name"]
        if repo.get("pinned_branch") is not None:
            active.add((repo_name, "branch_pinning"))
        if repo.get("protect_base_checkout") is True:
            active.add((repo_name, "protected_base_checkout"))
    for exception in repo_exceptions:
        identity = (exception["repo_name"], exception["policy"])
        if identity not in active:
            raise LeaseValidationError(
                "requested policy exception is not active in the project snapshot"
            )


def lease_has_policy_exception(
    lease: Mapping[str, Any],
    *,
    repo_name: str,
    policy: str,
) -> bool:
    """Return whether a canonical lease carries one exact repo-policy exception."""
    exceptions = lease.get("requested_policy_exceptions")
    if not isinstance(exceptions, list):
        return False
    return any(
        isinstance(exception, dict)
        and exception.get("repo_name") == repo_name
        and exception.get("policy") == policy
        for exception in exceptions
    )


def _application_hash(
    thread_id: str,
    user_id: str,
    application: dict[str, Any],
    *,
    requesting_project_id: str | None,
    repo_policy_snapshot: list[dict[str, Any]],
    repo_policy_hash: str,
) -> str:
    payload = {
        "version": _APPLICATION_VERSION,
        "requesting_session_id": thread_id,
        "requesting_project_id": requesting_project_id,
        "user_id": user_id,
        "application": application,
        "repo_policy_snapshot": repo_policy_snapshot,
        "repo_policy_hash": repo_policy_hash,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now(value: datetime | None) -> datetime:
    return value if value is not None else utc_now()


@contextmanager
def _transaction(db: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    connection = db if db is not None else get_db()
    owns_connection = db is None
    if connection.in_transaction:
        raise LeaseConflictError(
            "host-access mutations require a clean BEGIN IMMEDIATE boundary"
        )
    try:
        with write_transaction(connection, scope="host_access_lease"):
            yield connection
    finally:
        if owns_connection:
            connection.close()


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _public_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["plan"] = _decode_json(result.get("plan")) or []
    policy_snapshot = _decode_json(result.get("repo_policy_snapshot"))
    if not isinstance(policy_snapshot, list):
        raise LeaseLineageError("stored repository policy snapshot is malformed")
    result["repo_policy_snapshot"] = policy_snapshot
    policy_exceptions = _decode_json(result.get("requested_policy_exceptions"))
    if not isinstance(policy_exceptions, list):
        raise LeaseLineageError("stored policy exceptions are malformed")
    result["requested_policy_exceptions"] = policy_exceptions
    result["application_review_result"] = _decode_json(
        result.get("application_review_result")
    )
    review = result["application_review_result"]
    result["reviewer"] = (
        {
            "model": result.get("reviewer_model"),
            "display_name": review.get("reviewer_display_name"),
            "attestation": review.get("attestation"),
        }
        if result.get("reviewer_model") and isinstance(review, dict)
        else None
    )
    return result


_SELECT = (
    "SELECT l.*, s.title AS root_thread_title "
    "FROM host_access_leases l "
    "LEFT JOIN threads s ON s.id=l.root_thread_id "
)


def _lease_row(
    connection: sqlite3.Connection,
    lease_id: str,
    *,
    user_id: str | None = None,
) -> sqlite3.Row | None:
    sql = _SELECT + "WHERE l.id=?"
    params: list[Any] = [lease_id]
    if user_id is not None:
        sql += " AND l.user_id=?"
        params.append(user_id)
    return connection.execute(sql, params).fetchone()


def _expire_due(connection: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    now_iso = now.isoformat()
    rows = connection.execute(
        _SELECT
        + "WHERE l.state='active' AND l.expires_at IS NOT NULL "
        "AND l.expires_at<=? ORDER BY l.expires_at,l.id",
        (now_iso,),
    ).fetchall()
    expired: list[dict[str, Any]] = []
    for row in rows:
        changed = connection.execute(
            "UPDATE host_access_leases "
            "SET state='expired', terminal_reason='lease_expired', "
            "revision=revision+1, updated_at=? "
            "WHERE id=? AND state='active' AND revision=? "
            "AND application_hash=? AND expires_at<=?",
            (
                now_iso,
                row["id"],
                row["revision"],
                row["application_hash"],
                now_iso,
            ),
        )
        if changed.rowcount == 1:
            expired_row = _lease_row(connection, row["id"])
            if expired_row is not None:
                expired.append(_public_row(expired_row))
    return expired


def request_lease(
    thread_id: str,
    *,
    purpose: Any,
    plan: Any,
    duration_seconds: Any,
    expected_resources: Any = None,
    persistent_effects: Any = None,
    rollback_plan: Any = None,
    forbidden_effects: Any = None,
    requested_policy_exceptions: Any = None,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create an immutable reviewing lease for the exact requesting thread."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise LeaseValidationError("requesting session is required")
    thread_id = thread_id.strip()
    application = _normalize_application(
        purpose=purpose,
        plan=plan,
        duration_seconds=duration_seconds,
        expected_resources=expected_resources,
        persistent_effects=persistent_effects,
        rollback_plan=rollback_plan,
        forbidden_effects=forbidden_effects,
        requested_policy_exceptions=requested_policy_exceptions,
    )
    with _transaction(db) as connection:
        timestamp_value = _now(now)
        timestamp = timestamp_value.isoformat()
        _expire_due(connection, timestamp_value)
        session = connection.execute(
            "SELECT id,user_id,project_id FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if session is None or not session["user_id"]:
            raise LeaseLineageError("requesting session identity is unavailable")
        user_id = session["user_id"]
        requesting_project_id = session["project_id"]
        try:
            (
                repo_policy_snapshot,
                repo_policy_snapshot_json,
                repo_policy_hash,
            ) = load_project_repo_policy_snapshot(
                requesting_project_id,
                db=connection,
            )
        except RepoPolicyQueryError as exc:
            raise LeaseLineageError(str(exc)) from exc
        _validate_policy_exceptions(
            application["requested_policy_exceptions"],
            project_id=requesting_project_id,
            repo_policy_snapshot=repo_policy_snapshot,
        )
        digest = _application_hash(
            thread_id,
            user_id,
            application,
            requesting_project_id=requesting_project_id,
            repo_policy_snapshot=repo_policy_snapshot,
            repo_policy_hash=repo_policy_hash,
        )
        existing = connection.execute(
            _SELECT
            + "WHERE l.root_thread_id=? "
            "AND l.state IN ('reviewing','awaiting_user','activating','active')",
            (thread_id,),
        ).fetchone()
        if existing is not None:
            if existing["application_hash"] == digest:
                return _public_row(existing)
            raise LeaseConflictError(
                "the requesting thread already has a different open lease; "
                "release it with release_host_access before requesting a "
                "different scope"
            )

        lease_id = str(uuid4())
        connection.execute(
            "INSERT INTO host_access_leases "
            "(id,root_thread_id,user_id,state,purpose,plan,"
            "requested_duration_seconds,expected_resources,persistent_effects,"
            "rollback_plan,forbidden_effects,requesting_project_id,"
            "repo_policy_snapshot,repo_policy_hash,requested_policy_exceptions,"
            "application_hash,revision,"
            "created_at,updated_at) "
            "VALUES (?,?,?,'reviewing',?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (
                lease_id,
                thread_id,
                user_id,
                application["purpose"],
                json.dumps(
                    application["plan"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                application["duration_seconds"],
                application["expected_resources"],
                application["persistent_effects"],
                application["rollback_plan"],
                application["forbidden_effects"],
                requesting_project_id,
                repo_policy_snapshot_json,
                repo_policy_hash,
                json.dumps(
                    application["requested_policy_exceptions"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                digest,
                timestamp,
                timestamp,
            ),
        )
        row = _lease_row(connection, lease_id)
        assert row is not None
        return _public_row(row)


def get_lease(
    lease_id: str,
    *,
    user_id: str | None = None,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Read one lease, optionally hiding rows not owned by ``user_id``."""
    with _transaction(db) as connection:
        _expire_due(connection, _now(now))
        row = _lease_row(connection, lease_id, user_id=user_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        return _public_row(row)


def list_persisted_nonterminal_leases(
    *,
    db: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return every persisted nonterminal row without applying expiry.

    Startup uses this raw inventory to invalidate stale authority. Applying
    normal read-time expiry first would hide an ``active`` row from cleanup and
    leave its lease-owned processes behind.
    """
    connection = db if db is not None else get_db()
    owns_connection = db is None
    try:
        rows = connection.execute(
            _SELECT
            + "WHERE l.state IN "
            "('reviewing','awaiting_user','activating','active') "
            "ORDER BY l.created_at,l.id"
        ).fetchall()
        return [_public_row(row) for row in rows]
    finally:
        if owns_connection:
            connection.close()


def terminate_lease_projection(
    lease_id: str,
    *,
    expected_revision: int,
    application_hash: str,
    target_state: str,
    reason: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Project one already-fenced owner into an exact terminal DB state.

    This function deliberately does not expire other rows first. The
    process-private owner has already removed live authority; this is only its
    bounded durable projection and must not be redirected by read-time repair.
    """
    if target_state not in {"blocked", "expired", "revoked"}:
        raise LeaseValidationError("terminal projection state is invalid")
    terminal_reason = _text(
        reason,
        "reason",
        required=True,
        max_chars=MAX_TERMINAL_REASON_CHARS,
    )
    with _transaction(db) as connection:
        timestamp = _now(now)
        row = _lease_row(connection, lease_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states=NONTERMINAL_STATES,
            target_state=target_state,
            updates={"terminal_reason": terminal_reason},
            now=timestamp,
        )


def _assert_binding(
    row: sqlite3.Row,
    *,
    expected_revision: int,
    application_hash: str,
) -> None:
    if row["application_hash"] != application_hash:
        raise LeaseConflictError("application hash does not match")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise LeaseConflictError("expected revision must be an integer")
    if row["revision"] != expected_revision:
        raise LeaseConflictError("lease revision is stale")


def _transition(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    expected_revision: int,
    application_hash: str,
    allowed_states: set[str] | frozenset[str],
    target_state: str,
    updates: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    _assert_binding(
        row,
        expected_revision=expected_revision,
        application_hash=application_hash,
    )
    if row["state"] not in allowed_states:
        raise LeaseConflictError(
            f"cannot transition {row['state']} to {target_state}"
        )
    assignments = ["state=?", "revision=revision+1", "updated_at=?"]
    values: list[Any] = [target_state, now.isoformat()]
    for column, value in updates.items():
        assignments.append(f"{column}=?")
        values.append(value)
    values.extend(
        [
            row["id"],
            row["state"],
            expected_revision,
            application_hash,
        ]
    )
    changed = connection.execute(
        f"UPDATE host_access_leases SET {','.join(assignments)} "
        "WHERE id=? AND state=? AND revision=? AND application_hash=?",
        values,
    )
    if changed.rowcount != 1:
        raise LeaseConflictError("lease changed concurrently")
    result = _lease_row(connection, row["id"])
    assert result is not None
    return _public_row(result)


def _public_action_review(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["redacted_descriptor"] = _decode_json(
        result.get("redacted_descriptor")
    )
    return result


def record_action_review(
    lease_id: str,
    *,
    thread_id: str,
    lease_revision: int,
    tool_name: str,
    action_hash: str,
    redacted_descriptor: dict[str, Any],
    reviewer_model: str,
    verdict: str,
    reason: str,
    permit_expires_at: str | None = None,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Append one exact action-review audit row without granting authority."""
    session = _text(
        thread_id,
        "thread_id",
        required=True,
        max_chars=500,
    )
    tool = _text(
        tool_name,
        "tool_name",
        required=True,
        max_chars=MAX_TOOL_NAME_CHARS,
    )
    reviewer = _text(
        reviewer_model,
        "reviewer_model",
        required=True,
        max_chars=MAX_REVIEWER_MODEL_CHARS,
    )
    review_reason = _text(
        reason,
        "reason",
        required=True,
        max_chars=MAX_TERMINAL_REASON_CHARS,
    )
    if verdict not in {"allow", "block", "revoke"}:
        raise LeaseValidationError("verdict must be allow, block, or revoke")
    if isinstance(lease_revision, bool) or not isinstance(lease_revision, int):
        raise LeaseValidationError("lease_revision must be an integer")
    if (
        not isinstance(action_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", action_hash) is None
    ):
        raise LeaseValidationError(
            "action_hash must be a lowercase SHA-256 digest"
        )
    if not isinstance(redacted_descriptor, dict):
        raise LeaseValidationError("redacted_descriptor must be an object")
    try:
        validate_action_descriptor(tool, redacted_descriptor)
    except CommandRedactionError as exc:
        raise LeaseValidationError(
            "redacted_descriptor is invalid"
        ) from exc
    encoded_descriptor = json.dumps(
        redacted_descriptor,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded_descriptor) > MAX_ACTION_DESCRIPTOR_CHARS:
        raise LeaseValidationError(
            "redacted_descriptor exceeds the durable review limit"
        )

    permit_deadline = None
    if verdict == "allow":
        permit_deadline = _text(
            permit_expires_at,
            "permit_expires_at",
            required=True,
            max_chars=100,
        )
        try:
            datetime.fromisoformat(permit_deadline)
        except (TypeError, ValueError) as exc:
            raise LeaseValidationError(
                "permit_expires_at must be an ISO-8601 timestamp"
            ) from exc
    with _transaction(db) as connection:
        timestamp = _now(now)
        review_id = str(uuid4())
        permit_id = str(uuid4()) if verdict == "allow" else None
        connection.execute(
            "INSERT INTO host_access_action_reviews "
            "(id,lease_id,lease_revision,thread_id,tool_name,action_hash,"
            "redacted_descriptor,reviewer_model,verdict,reason,permit_id,"
            "permit_expires_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id,
                lease_id,
                lease_revision,
                session,
                tool,
                action_hash,
                encoded_descriptor,
                reviewer,
                verdict,
                review_reason,
                permit_id,
                permit_deadline,
                timestamp.isoformat(),
            ),
        )
        review_row = connection.execute(
            "SELECT * FROM host_access_action_reviews WHERE id=?",
            (review_id,),
        ).fetchone()
        assert review_row is not None
        return _public_action_review(review_row)


def mark_action_permit_consumed(
    permit_id: str,
    *,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> bool:
    """Best-effort audit marker; process-private owner state is authority."""
    permit = _text(
        permit_id,
        "permit_id",
        required=True,
        max_chars=500,
    )
    with _transaction(db) as connection:
        timestamp = _now(now)
        changed = connection.execute(
            "UPDATE host_access_action_reviews SET permit_consumed_at=? "
            "WHERE permit_id=? AND permit_consumed_at IS NULL",
            (timestamp.isoformat(), permit),
        )
        return changed.rowcount == 1


def record_application_review(
    lease_id: str,
    *,
    expected_revision: int,
    application_hash: str,
    allowed: bool,
    auto_approve: bool = False,
    isolation_failed: bool = False,
    reviewer_model: str,
    review_result: dict[str, Any],
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record the independent application decision."""
    if not isinstance(allowed, bool):
        raise LeaseValidationError("application review verdict must be boolean")
    if not isinstance(auto_approve, bool):
        raise LeaseValidationError("automatic approval mode must be boolean")
    if not isinstance(isolation_failed, bool) or (isolation_failed and not allowed):
        raise LeaseValidationError("browser isolation failure requires an allowed review")
    reviewer = _text(
        reviewer_model,
        "reviewer_model",
        required=True,
        max_chars=MAX_REVIEWER_MODEL_CHARS,
    )
    if not isinstance(review_result, dict):
        raise LeaseValidationError("review_result must be an object")
    encoded_result = encode_application_review(review_result, auto_approve=auto_approve)
    if isolation_failed:
        target = "blocked"
        terminal_reason = "browser_isolation_failed"
    elif not allowed:
        target = "rejected"
        terminal_reason = "application_review_rejected"
    elif auto_approve:
        target = "activating"
        terminal_reason = None
    else:
        target = "awaiting_user"
        terminal_reason = None
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        if (
            row["state"] == target
            and row["reviewer_model"] == reviewer
            and row["application_review_result"] == encoded_result
            and row["application_hash"] == application_hash
            and row["terminal_reason"] == terminal_reason
        ):
            return _public_row(row)
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states={"reviewing"},
            target_state=target,
            updates={
                "reviewer_model": reviewer,
                "application_review_result": encoded_result,
                "application_reviewed_at": timestamp.isoformat(),
                "terminal_reason": terminal_reason,
            },
            now=timestamp,
        )


def _user_decision(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
    decision: str,
    now: datetime | None,
    db: sqlite3.Connection | None,
) -> dict[str, Any]:
    target = "activating" if decision == "approved" else "rejected"
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id, user_id=user_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        if row["application_hash"] != application_hash:
            raise LeaseConflictError("application hash does not match")
        if row["user_decision"] == decision:
            return _public_row(row)
        if row["user_decision"] is not None:
            raise LeaseConflictError("lease already has a different user decision")
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states={"awaiting_user"},
            target_state=target,
            updates={
                "user_decision": decision,
                "user_decided_at": timestamp.isoformat(),
                "terminal_reason": (
                    None if decision == "approved" else "user_rejected"
                ),
            },
            now=timestamp,
        )


def approve_lease(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Move an independently reviewed lease to supervisor activation."""
    return _user_decision(
        lease_id,
        user_id=user_id,
        expected_revision=expected_revision,
        application_hash=application_hash,
        decision="approved",
        now=now,
        db=db,
    )


def reject_lease(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Reject a pending reviewed application without granting access."""
    return _user_decision(
        lease_id,
        user_id=user_id,
        expected_revision=expected_revision,
        application_hash=application_hash,
        decision="rejected",
        now=now,
        db=db,
    )


def activate_lease(
    lease_id: str,
    *,
    expected_revision: int,
    application_hash: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Start the lease clock only after the supervisor is healthy."""
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        root = connection.execute(
            "SELECT user_id FROM threads WHERE id=?",
            (row["root_thread_id"],),
        ).fetchone()
        if root is None or root["user_id"] != row["user_id"]:
            # Identity invalidation is an authoritative safety repair, not a
            # replay of the caller's stale activation transition. Bind the
            # fail-closed CAS to the row just read under the write boundary.
            return _transition(
                connection,
                row,
                expected_revision=row["revision"],
                application_hash=row["application_hash"],
                allowed_states={"activating", "active"},
                target_state="blocked",
                updates={"terminal_reason": "root_identity_unavailable"},
                now=timestamp,
            )
        if row["state"] == "active" and row["application_hash"] == application_hash:
            return _public_row(row)
        expires_at = (
            timestamp + timedelta(seconds=row["requested_duration_seconds"])
        ).isoformat()
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states={"activating"},
            target_state="active",
            updates={
                "activated_at": timestamp.isoformat(),
                "expires_at": expires_at,
            },
            now=timestamp,
        )


def record_activation_wake_delivery(
    lease_id: str,
    *,
    expected_revision: int,
    application_hash: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Confirm that the sole activation wake reached provider acceptance."""
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        _assert_binding(
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
        )
        if row["state"] != "active":
            raise LeaseConflictError(
                f"activation wake requires active lease; state is {row['state']}"
            )
        if row["activation_wake_delivered_at"] is not None:
            return _public_row(row)
        changed = connection.execute(
            "UPDATE host_access_leases "
            "SET activation_wake_delivered_at=?,updated_at=? "
            "WHERE id=? AND state='active' AND revision=? "
            "AND application_hash=? AND activation_wake_delivered_at IS NULL",
            (
                timestamp.isoformat(),
                timestamp.isoformat(),
                lease_id,
                expected_revision,
                application_hash,
            ),
        )
        if changed.rowcount != 1:
            raise LeaseConflictError(
                "activation wake confirmation changed concurrently"
            )
        delivered = _lease_row(connection, lease_id)
        assert delivered is not None
        return _public_row(delivered)


def revoke_lease(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Immediately revoke an activating or active lease."""
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id, user_id=user_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        if (
            row["state"] == "revoked"
            and row["terminal_reason"] == "user_revoked"
            and row["application_hash"] == application_hash
        ):
            return _public_row(row)
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states={"activating", "active"},
            target_state="revoked",
            updates={"terminal_reason": "user_revoked"},
            now=timestamp,
        )


def get_open_lease_for_session(
    thread_id: str,
    *,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return the requesting thread's own open lease, or ``None``.

    Exactly one nonterminal lease can exist per root session (enforced by a
    unique partial index), so this resolves the caller's own lease without
    trusting a caller-supplied lease id.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise LeaseValidationError("requesting session is required")
    with _transaction(db) as connection:
        _expire_due(connection, _now(now))
        row = connection.execute(
            _SELECT
            + "WHERE l.root_thread_id=? "
            "AND l.state IN ('reviewing','awaiting_user','activating','active')",
            (thread_id.strip(),),
        ).fetchone()
        return _public_row(row) if row is not None else None


def release_lease(
    lease_id: str,
    *,
    root_thread_id: str,
    expected_revision: int,
    application_hash: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """End the requesting thread's own lease early, from any live state.

    This is the holder giving up its own authority, so it is recorded as
    ``agent_released`` rather than a user revocation. It is bound to the exact
    root session, so one thread can never end another thread's lease.
    """
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id)
        if row is None or row["root_thread_id"] != root_thread_id:
            raise LeaseNotFoundError("host-access lease not found")
        if (
            row["state"] == "revoked"
            and row["terminal_reason"] == "agent_released"
            and row["application_hash"] == application_hash
        ):
            return _public_row(row)
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states=NONTERMINAL_STATES,
            target_state="revoked",
            updates={"terminal_reason": "agent_released"},
            now=timestamp,
        )


def block_lease(
    lease_id: str,
    *,
    expected_revision: int,
    application_hash: str,
    reason: str,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Fail closed from any nonterminal state."""
    terminal_reason = _text(
        reason,
        "reason",
        required=True,
        max_chars=MAX_TERMINAL_REASON_CHARS,
    )
    with _transaction(db) as connection:
        timestamp = _now(now)
        _expire_due(connection, timestamp)
        row = _lease_row(connection, lease_id)
        if row is None:
            raise LeaseNotFoundError("host-access lease not found")
        if (
            row["state"] == "blocked"
            and row["terminal_reason"] == terminal_reason
            and row["application_hash"] == application_hash
        ):
            return _public_row(row)
        return _transition(
            connection,
            row,
            expected_revision=expected_revision,
            application_hash=application_hash,
            allowed_states=NONTERMINAL_STATES,
            target_state="blocked",
            updates={"terminal_reason": terminal_reason},
            now=timestamp,
        )


from infra.host_access_lease_reads import list_leases  # noqa: E402,F401
