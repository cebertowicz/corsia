"""Artifact reconciliation: preview (dry-run) and apply (idempotent repair).

Preview is read-only. Apply is owner-scoped, local-filesystem-only, and
revalidates the exact path and identity version immediately before mutating
lifecycle — only an authoritative ENOENT/ENOTDIR result qualifies; any other
I/O outcome is a visible skip and never a mutation. It never deletes a file
and never infers relocation. Every attempt produces a durable receipt keyed by
(artifact, identity version, owner session); only an ``applied`` receipt acts as
an idempotency barrier, so a replayed apply never double-mutates while a
transient I/O failure or a lost race never permanently blocks a later repair.
An unresolved receipt is cleared again as soon as its path verifies healthy, so
a transient failure cannot outlive its cause as a permanent needs-attention row.
Ephemeral identities are tombstoned on apply; durable identities are archived.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from loguru import logger

from infra.database import TransactionOwnershipError, get_db, write_transaction
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.registration_events import _now
from registry import models
from registry.service import notify_changed


def _owner_scoped_candidates(db, thread_id: str) -> list[dict]:
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""SELECT i.id, i.integration, i.type, i.external_id, i.title,
                   i.path, i.state, i.retention, i.updated_at
            FROM artifact_identities i
            JOIN artifact_registrations r ON r.artifact_id = i.id
            WHERE r.parent_thread_id=?
              AND i.state='active'
              AND r.relationship_state IN ({placeholders})
              AND i.path IS NOT NULL AND i.path != ''
            ORDER BY i.id""",
        (thread_id, *OWNER_REGISTRATION_STATES),
    ).fetchall()
    return [dict(row) for row in rows]


def _is_local_path(path: str | None) -> bool:
    return path is not None and bool(path) and "://" not in path


def _missing_authoritative(path: str) -> tuple[bool, str | None]:
    """Return ``(missing, io_error)``. ``missing`` is True only for an
    authoritative ENOENT/ENOTDIR; any other OSError is reported, never
    treated as missing."""
    try:
        os.stat(path)
        return False, None
    except FileNotFoundError:
        return True, None
    except NotADirectoryError:
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def preview_for_thread(thread_id: str) -> dict:
    """Return a dry-run of known artifact pane repairs for a thread."""
    db = get_db()
    try:
        candidates = _owner_scoped_candidates(db, thread_id)
    finally:
        db.close()

    actions = []
    for c in candidates:
        if not _is_local_path(c["path"]):
            continue
        missing, _io_error = _missing_authoritative(c["path"])
        if not missing:
            continue
        actions.append({
            "action": "tombstone" if c["retention"] == "ephemeral" else "archive",
            "artifact_id": c["id"],
            "name": c.get("title") or c.get("external_id"),
            "path": c["path"],
            "reason": "file path no longer exists",
        })
    return {
        "ok": True,
        "mode": "preview",
        "dry_run": True,
        "counts": {"missing": len(actions)},
        "actions": actions,
    }


def _find_receipt(
    db, *, artifact_id: str, identity_version: str, thread_id: str,
    status: str | None = None,
) -> dict | None:
    """Return this key's receipt, optionally only when it has ``status``."""
    params: tuple[str, ...] = (artifact_id, identity_version, thread_id)
    clause = ""
    if status is not None:
        clause = " AND status=?"
        params = (*params, status)
    row = db.execute(
        "SELECT * FROM artifact_reconcile_receipts "
        "WHERE artifact_id=? AND identity_version=? AND thread_id=?" + clause,
        params,
    ).fetchone()
    return dict(row) if row else None


_RECEIPT_BATCH_SIZE = 400


def _receipts_for_candidates(
    db, thread_id: str, candidates: list[dict[str, Any]],
) -> dict[tuple[str, str], dict]:
    """Return receipts only for candidates in this reconcile pass.

    A healthy pane read commonly reconciles thousands of existing files. Read
    receipts in bounded batches rather than once per successful ``stat`` or
    loading unrelated applied receipt history for the owner. Missing candidates
    still revalidate their idempotency barrier in the write transaction.
    """
    artifact_ids = [candidate["id"] for candidate in candidates]
    receipts: dict[tuple[str, str], dict] = {}
    for start in range(0, len(artifact_ids), _RECEIPT_BATCH_SIZE):
        batch = artifact_ids[start:start + _RECEIPT_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        rows = db.execute(
            "SELECT * FROM artifact_reconcile_receipts "
            f"WHERE thread_id=? AND artifact_id IN ({placeholders})",
            (thread_id, *batch),
        ).fetchall()
        receipts.update({
            (row["artifact_id"], row["identity_version"]): dict(row)
            for row in rows
        })
    return receipts


def _write_receipt(
    db, *, artifact_id: str, identity_version: str, thread_id: str,
    action: str, status: str, reason: str | None,
) -> dict:
    """Record this attempt's outcome as the receipt for its key.

    ``applied`` is terminal: it is the idempotency barrier and is never
    overwritten, so a replayed apply can never re-mutate lifecycle. A
    ``failed``/``skipped`` receipt records the latest *unresolved* attempt and
    is replaced in place — a transient EACCES/EIO or a lost path-recreation
    race stays visible without ever becoming a permanent block on repairing
    that artifact. Returns the row that is actually stored afterwards.
    """
    with write_transaction(db, scope="reconcile_receipt"):
        db.execute(
            "INSERT INTO artifact_reconcile_receipts "
            "(id, artifact_id, identity_version, thread_id, action, status, "
            " reason, created_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(artifact_id, identity_version, thread_id) DO UPDATE SET "
            "  action=excluded.action, status=excluded.status, "
            "  reason=excluded.reason, created_at=excluded.created_at "
            "WHERE artifact_reconcile_receipts.status != 'applied'",
            (
                str(uuid.uuid4())[:12], artifact_id, identity_version, thread_id,
                action, status, reason, _now(),
            ),
        )
        stored = _find_receipt(
            db, artifact_id=artifact_id, identity_version=identity_version,
            thread_id=thread_id,
        )
        if stored is None:  # pragma: no cover - the upsert above guarantees a row
            raise RuntimeError(
                f"reconcile receipt vanished after write: {artifact_id}"
            )
        return stored


def _clear_unresolved_receipt(
    db, *, artifact_id: str, identity_version: str, thread_id: str,
    known_receipt: dict | None,
) -> None:
    """Drop this key's unresolved receipt once its path verifies healthy.

    ``_write_receipt`` only ever replaces an unresolved receipt on a *later*
    attempt that has something new to record. The most likely outcome of a
    transient EACCES/EIO — a file that was never actually missing — produces
    no such attempt: the candidate simply verifies healthy on every later
    pass. Without this the ``failed`` receipt outlives its cause forever,
    because ``identity_version`` is ``i.updated_at`` and nothing on this path
    changes it, so ``registry.view`` keeps matching it and the user is left
    with a permanent "needs attention" row whose Retry can never clear it.

    Reads before writing: a healthy candidate is the common case on every
    pane refresh, and it must not cost a commit. ``applied`` is terminal and
    is never removed.
    """
    if known_receipt is None or known_receipt["status"] == "applied":
        return
    with write_transaction(db, scope="reconcile_clear_receipt"):
        db.execute(
            "DELETE FROM artifact_reconcile_receipts "
            "WHERE artifact_id=? AND identity_version=? AND thread_id=? "
            "  AND status != 'applied'",
            (artifact_id, identity_version, thread_id),
        )


def _current_candidate(db, c: dict[str, Any], thread_id: str):
    """Revalidate the scanned identity and its current owner using DB state only."""
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    return db.execute(
        f"""SELECT i.path, i.retention FROM artifact_identities i
            JOIN artifact_registrations r ON r.artifact_id=i.id
            WHERE i.id=? AND i.state='active' AND i.path=? AND i.updated_at=?
              AND r.parent_thread_id=?
              AND r.relationship_state IN ({placeholders})""",
        (c["id"], c["path"], c["updated_at"], thread_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()


def _retire_missing_candidate(
    db, c: dict[str, Any], thread_id: str,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Commit retirement and its receipt together after the final filesystem check."""
    with write_transaction(db, scope="reconcile_retire"):
        applied = _find_receipt(
            db, artifact_id=c["id"], identity_version=c["updated_at"],
            thread_id=thread_id, status="applied",
        )
        if applied is not None:
            return applied, None
        current = _current_candidate(db, c, thread_id)
        if current is None:
            return _write_receipt(
                db, artifact_id=c["id"], identity_version=c["updated_at"],
                thread_id=thread_id, action="skip", status="skipped",
                reason="identity changed since scan",
            ), None
        action = "tombstone" if current["retention"] == "ephemeral" else "archive"
        models.retire(c["id"], tombstone=(action == "tombstone"), db=db)
        receipt = _write_receipt(
            db, artifact_id=c["id"], identity_version=c["updated_at"],
            thread_id=thread_id, action=action, status="applied", reason=None,
        )
        return receipt, {
            "integration": c["integration"], "type": c["type"],
            "external_id": c["external_id"],
        }


def _apply_one_candidate(
    db, c: dict[str, Any], thread_id: str,
    receipts_by_candidate: dict[tuple[str, str], dict],
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Apply (or skip/fail) one missing-file candidate. Returns
    ``(receipt_or_None, applied_identity_or_None)``; ``receipt`` is None only
    when the candidate is not authoritative-missing and had no prior I/O
    error (nothing left to record)."""
    if not _is_local_path(c["path"]):
        return None, None
    known_receipt = receipts_by_candidate.get((c["id"], c["updated_at"]))
    missing, io_error = _missing_authoritative(c["path"])
    if not missing:
        if not io_error:
            # Verified healthy: this is the only signal that an earlier
            # unresolved failure against this identity version is over.
            _clear_unresolved_receipt(
                db, artifact_id=c["id"], identity_version=c["updated_at"],
                thread_id=thread_id, known_receipt=known_receipt,
            )
            return None, None
        return _write_receipt(
            db, artifact_id=c["id"], identity_version=c["updated_at"],
            thread_id=thread_id, action="skip", status="failed",
            reason=io_error,
        ), None

    # Only an *applied* receipt is an idempotency barrier. ``identity_version``
    # is ``i.updated_at``, which does not change when a backing file
    # disappears, so treating a failed/skipped receipt as a barrier would
    # permanently strand this artifact at that version. The mutation helper
    # repeats this check inside its transaction to cover a receipt written after
    # this pass took its bounded snapshot.
    if known_receipt is not None and known_receipt["status"] == "applied":
        return known_receipt, None

    # Revalidate identity version immediately before mutating: a concurrent
    # change since the candidate scan invalidates this pass.
    current = _current_candidate(db, c, thread_id)
    if current is None:
        return _write_receipt(
            db, artifact_id=c["id"], identity_version=c["updated_at"],
            thread_id=thread_id, action="skip", status="skipped",
            reason="identity changed since scan",
        ), None

    missing_again, io_error = _missing_authoritative(current["path"])
    if not missing_again:
        return _write_receipt(
            db, artifact_id=c["id"], identity_version=c["updated_at"],
            thread_id=thread_id, action="skip",
            status="failed" if io_error else "skipped",
            reason=io_error or "path recreated",
        ), None

    return _retire_missing_candidate(db, c, thread_id)


def _contain_candidate_failure(
    db, c: dict[str, Any], thread_id: str, exc: BaseException,
) -> dict[str, Any] | None:
    """Record one candidate's unexpected failure so the pass can continue.

    A single bad row (unreadable mount, corrupt identity, receipt write
    contention) must not abort repair for every other artifact in the thread.
    """
    logger.warning(f"reconcile apply failed for artifact {c['id']}: {exc!r}")
    try:
        return _write_receipt(
            db, artifact_id=c["id"], identity_version=c["updated_at"],
            thread_id=thread_id, action="skip", status="failed",
            reason=f"{type(exc).__name__}: {exc}",
        )
    except Exception as receipt_exc:
        logger.warning(
            f"reconcile could not record failure for {c['id']}: {receipt_exc!r}"
        )
        return None


def apply_for_thread(thread_id: str) -> dict:
    """Idempotently repair one thread's owner-scoped missing local artifacts."""
    db = get_db()
    if db.in_transaction:
        raise TransactionOwnershipError(
            "reconcile_apply requires a clean connection before filesystem checks"
        )
    try:
        candidates = _owner_scoped_candidates(db, thread_id)
        receipts_by_candidate = _receipts_for_candidates(db, thread_id, candidates)
        receipts: list[dict[str, Any]] = []
        applied_identities: list[dict[str, str]] = []
        for c in candidates:
            try:
                receipt, applied_identity = _apply_one_candidate(
                    db, c, thread_id, receipts_by_candidate,
                )
            except Exception as exc:
                receipt = _contain_candidate_failure(db, c, thread_id, exc)
                applied_identity = None
            if receipt is not None:
                receipts.append(receipt)
            if applied_identity is not None:
                applied_identities.append(applied_identity)
    finally:
        db.close()

    # notify_changed strictly after every commit above — no precommit broadcast.
    for identity in applied_identities:
        notify_changed(identity)

    return {
        "ok": True,
        "mode": "apply",
        "dry_run": False,
        "counts": {
            # Work this pass actually did, not rows that happen to sit in an
            # applied state: a replay against an existing applied receipt
            # mutates nothing, so two concurrent passes must not both claim
            # the same single repair. Skipped/failed receipts are always
            # written by this pass, so they already count only its own work.
            "applied": len(applied_identities),
            "skipped": sum(1 for r in receipts if r["status"] == "skipped"),
            "failed": sum(1 for r in receipts if r["status"] == "failed"),
        },
        "receipts": receipts,
    }
