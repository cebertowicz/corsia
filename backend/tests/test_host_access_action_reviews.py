from __future__ import annotations

from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
import sqlite3

import pytest


def _db(path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _session(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    user_id: str = "steve",
    spawned_by: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,spawned_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (thread_id, user_id, thread_id, spawned_by, "t", "t"),
    )
    db.commit()


def _active_lease(
    db: sqlite3.Connection,
    *,
    duration_seconds: int = 60,
) -> dict:
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        record_application_review,
        request_lease,
    )

    requested = request_lease(
        "root",
        purpose="Inspect the host",
        plan=["Read process state"],
        duration_seconds=duration_seconds,
        db=db,
    )
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="claude-code/opus",
        review_result={"attestation": "Bounded and reviewable."},
        db=db,
    )
    approved = approve_lease(
        requested["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )
    return activate_lease(
        requested["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        now=datetime(2026, 7, 18, 12, 0, 0),
        db=db,
    )


def _record(db, lease, **overrides):
    from infra.host_access_leases import record_action_review

    values = {
        "lease_id": lease["id"],
        "thread_id": "child",
        "lease_revision": lease["revision"],
        "tool_name": "bash",
        "action_hash": "a" * 64,
        "redacted_descriptor": {
            "version": 1,
            "tool": "bash",
            "command": {
                "tokens": ["ps", "-ef"],
                "redactions": 0,
                "literal_credentials": 0,
            },
            "working_directory": None,
            "timeout_seconds": 120,
            "background": False,
        },
        "reviewer_model": "claude-code/opus",
        "verdict": "allow",
        "reason": "Matches the approved diagnostic purpose.",
        "permit_expires_at": "2026-07-18T12:00:11",
        "now": datetime(2026, 7, 18, 12, 0, 1),
        "db": db,
    }
    values.update(overrides)
    return record_action_review(**values)


def test_action_review_is_bound_and_append_only(isolated_db):
    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)

    result = _record(db, lease)
    review = result

    assert review["lease_id"] == lease["id"]
    assert review["lease_revision"] == lease["revision"]
    assert review["thread_id"] == "child"
    assert review["redacted_descriptor"] == {
        "command": {
            "tokens": ["ps", "-ef"],
            "redactions": 0,
            "literal_credentials": 0,
        },
        "working_directory": None,
        "timeout_seconds": 120,
        "background": False,
        "tool": "bash",
        "version": 1,
    }
    assert review["verdict"] == "allow"
    assert isinstance(review["permit_id"], str)
    assert review["permit_expires_at"] == "2026-07-18T12:00:11"
    assert review["permit_consumed_at"] is None
    row = db.execute(
        "SELECT state,revision FROM host_access_leases WHERE id=?",
        (lease["id"],),
    ).fetchone()
    assert tuple(row) == ("active", lease["revision"])
    db.close()


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "version": 1,
            "tool": "bash",
            "command": "curl --user alice:raw-secret https://example.test",
        },
        {
            "version": 1,
            "tool": "bash",
            "command": {
                "tokens": ["printf", "ok"],
                "redactions": 0,
                "literal_credentials": 0,
            },
            "raw_command": "curl --user alice:raw-secret https://example.test",
        },
    ],
)
def test_action_review_rejects_unredacted_bash_descriptor(
    isolated_db,
    descriptor,
):
    from infra.host_access_leases import LeaseValidationError

    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)

    with pytest.raises(
        LeaseValidationError,
        match="redacted_descriptor is invalid",
    ):
        _record(
            db,
            lease,
            redacted_descriptor=descriptor,
        )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM host_access_action_reviews"
        ).fetchone()[0]
        == 0
    )
    db.close()


def test_bash_action_review_persists_only_redacted_command(
    isolated_db,
    tmp_path,
):
    from tools import host_access_gate

    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)
    command = (
        "curl --user alice:$AUDIT_PASSWORD "
        "'https://example.test/private?token=$QUERY_TOKEN'"
    )
    action = host_access_gate._prepare_action(
        "bash",
        {"command": command},
        tool=SimpleNamespace(
            working_dir=str(tmp_path),
            default_timeout=120,
        ),
    )
    assert action is not None

    result = _record(
        db,
        lease,
        action_hash=action.action_hash,
        redacted_descriptor=action.review_descriptor(),
    )
    raw_descriptor = db.execute(
        "SELECT redacted_descriptor FROM host_access_action_reviews "
        "WHERE id=?",
        (result["id"],),
    ).fetchone()[0]

    assert "AUDIT_PASSWORD" not in raw_descriptor
    assert "QUERY_TOKEN" not in raw_descriptor
    assert json.loads(raw_descriptor)["command"] == {
        "tokens": [
            "curl",
            "--user",
            "alice:[REDACTED]",
            "https://example.test/private?token=[REDACTED]",
        ],
        "redactions": 2,
        "literal_credentials": 0,
    }
    db.close()


def test_bash_action_review_persists_verbatim_heredoc_body(
    isolated_db,
    tmp_path,
):
    from tools import host_access_gate

    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)
    command = (
        "python3 - <<'PY'\n"
        "print(\"a b\", 'c')\n"
        "PY\n"
        "curl --user alice:$AUDIT_PASSWORD https://example.test"
    )
    action = host_access_gate._prepare_action(
        "bash",
        {"command": command},
        tool=SimpleNamespace(
            working_dir=str(tmp_path),
            default_timeout=120,
        ),
    )
    assert action is not None

    result = _record(
        db,
        lease,
        action_hash=action.action_hash,
        redacted_descriptor=action.review_descriptor(),
    )
    raw_descriptor = db.execute(
        "SELECT redacted_descriptor FROM host_access_action_reviews "
        "WHERE id=?",
        (result["id"],),
    ).fetchone()[0]

    assert "AUDIT_PASSWORD" not in raw_descriptor
    assert json.loads(raw_descriptor)["command"] == {
        "tokens": [
            "python3",
            "-",
            "<<",
            "PY",
            ";",
            "curl",
            "--user",
            "alice:[REDACTED]",
            "https://example.test",
        ],
        "redactions": 1,
        "literal_credentials": 0,
        "heredocs": [
            {
                "delimiter": "PY",
                "quoted_delimiter": True,
                "body": "print(\"a b\", 'c')",
            },
        ],
    }
    db.close()


def test_non_bash_action_review_sanitizes_descriptor_but_hashes_raw_payload(
    isolated_db,
):
    from tools import host_access_gate

    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)

    def action(secret: str):
        payload = {
            "path": {
                "lexical": "/outside/service.log",
                "resolved": "/outside/service.log",
            },
            "pattern": (
                f"Authorization: Bearer {secret}\n"
                f"Cookie: session={secret}; theme=dark\n"
                f"postgresql://alice:{secret}@db.example/app\n"
                f"token={secret}\n"
                "-----BEGIN PRIVATE KEY-----\n"
                f"{secret}\n"
                "-----END PRIVATE KEY-----"
            ),
            "output_mode": "content",
        }
        prepared = host_access_gate._make_prepared(
            "grep",
            payload,
            requires_elevation=True,
        )
        encoded_raw = json.dumps(
            {"version": 1, "tool": "grep", **payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        assert prepared.action_hash == hashlib.sha256(
            encoded_raw.encode("utf-8")
        ).hexdigest()
        return prepared

    first = action("first-raw-secret")
    second = action("second-raw-secret")
    assert first.review_descriptor() == second.review_descriptor()
    assert first.action_hash != second.action_hash

    result = _record(
        db,
        lease,
        tool_name="grep",
        action_hash=first.action_hash,
        redacted_descriptor=first.review_descriptor(),
    )
    raw_descriptor = db.execute(
        "SELECT redacted_descriptor FROM host_access_action_reviews "
        "WHERE id=?",
        (result["id"],),
    ).fetchone()[0]

    assert "first-raw-secret" not in raw_descriptor
    assert "second-raw-secret" not in raw_descriptor
    assert "[REDACTED]" in raw_descriptor
    assert "/outside/service.log" in raw_descriptor
    assert '"output_mode":"content"' in raw_descriptor
    db.close()


@pytest.mark.parametrize(
    "verdict",
    ["block", "revoke"],
)
def test_action_verdict_audit_never_mutates_lease_authority(
    isolated_db,
    verdict,
):
    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)

    result = _record(db, lease, verdict=verdict)

    assert result["verdict"] == verdict
    assert result["permit_id"] is None
    stored = db.execute(
        "SELECT state,revision FROM host_access_leases WHERE id=?",
        (lease["id"],),
    ).fetchone()
    assert tuple(stored) == ("active", lease["revision"])
    db.close()


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"lease_revision": "invalid"}, "lease_revision"),
        ({"action_hash": "not-a-hash"}, "action_hash"),
        ({"verdict": "maybe"}, "verdict"),
    ],
)
def test_action_review_failures_append_nothing(
    isolated_db,
    override,
    error,
):
    from infra.host_access_leases import LeaseError

    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)

    with pytest.raises(LeaseError, match=error):
        _record(db, lease, **override)

    assert db.execute(
        "SELECT count(*) FROM host_access_action_reviews"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT state FROM host_access_leases WHERE id=?",
        (lease["id"],),
    ).fetchone()[0] == "active"
    db.close()


def test_consumed_marker_is_a_narrow_idempotent_audit_update(isolated_db):
    from infra.host_access_leases import mark_action_permit_consumed

    db = _db(isolated_db)
    _session(db, "root")
    _session(db, "child", spawned_by="root")
    lease = _active_lease(db)
    review = _record(db, lease)

    assert mark_action_permit_consumed(
        review["permit_id"],
        now=datetime(2026, 7, 18, 12, 0, 2),
        db=db,
    ) is True
    assert mark_action_permit_consumed(
        review["permit_id"],
        now=datetime(2026, 7, 18, 12, 0, 3),
        db=db,
    ) is False
    consumed_at = db.execute(
        "SELECT permit_consumed_at FROM host_access_action_reviews "
        "WHERE permit_id=?",
        (review["permit_id"],),
    ).fetchone()[0]
    assert consumed_at == "2026-07-18T12:00:02"
    db.close()
