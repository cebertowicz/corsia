from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
import sqlite3
import threading

import pytest


def _connection(path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _session(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    user_id: str = "steve",
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
    project_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,spawned_by,parent_thread_id,project_id,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,'t','t')",
        (
            thread_id,
            user_id,
            thread_id,
            spawned_by,
            parent_thread_id,
            project_id,
        ),
    )
    db.commit()


def _project(db: sqlite3.Connection, project_id: str) -> None:
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
        (project_id, project_id, "t", "t"),
    )
    db.commit()


def _application(**overrides):
    values = {
        "purpose": " Diagnose the local runtime ",
        "plan": [" Inspect processes ", " Read logs "],
        "duration_seconds": 120,
        "expected_resources": " process table ",
        "persistent_effects": " ",
        "rollback_plan": None,
        "forbidden_effects": " no restarts ",
        "requested_policy_exceptions": [],
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", ""),
        ("purpose", "  "),
        ("plan", []),
        ("plan", ["ok", " "]),
        ("plan", "not-a-list"),
        ("duration_seconds", True),
        ("duration_seconds", 0),
        ("duration_seconds", 1801),
        ("duration_seconds", 1.5),
        ("purpose", "x" * 2001),
        ("plan", ["x"] * 21),
        ("plan", ["x" * 1001]),
        ("expected_resources", "x" * 2001),
    ],
)
def test_application_validation_fails_closed(isolated_db, field, value):
    from infra.host_access_leases import LeaseValidationError, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    application = _application(**{field: value})

    with pytest.raises(LeaseValidationError):
        request_lease("root", db=db, **application)

    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.close()


def test_request_normalizes_hashes_and_is_idempotent(isolated_db):
    from infra.host_access_leases import request_lease

    db = _connection(isolated_db)
    _session(db, "root")

    first = request_lease("root", db=db, **_application())
    repeated = request_lease(
        "root",
        db=db,
        **_application(
            purpose="Diagnose the local runtime",
            plan=["Inspect processes", "Read logs"],
            expected_resources="process table",
            forbidden_effects="no restarts",
        ),
    )

    assert repeated["id"] == first["id"]
    assert first["state"] == "reviewing"
    assert first["root_thread_id"] == "root"
    assert first["user_id"] == "steve"
    assert first["purpose"] == "Diagnose the local runtime"
    assert first["plan"] == ["Inspect processes", "Read logs"]
    assert first["persistent_effects"] is None
    assert first["requested_policy_exceptions"] == []
    assert len(first["application_hash"]) == 64
    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 1
    db.close()


def test_list_leases_reads_without_a_writer_when_no_expiry_is_due(isolated_db):
    from infra.host_access_leases import list_leases, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    statements: list[str] = []
    db.set_trace_callback(statements.append)

    listed = list_leases("steve", now=datetime(2026, 7, 18, 12, 0, 0), db=db)

    assert listed == [requested]
    assert not any(
        statement.lstrip().upper().startswith("BEGIN IMMEDIATE")
        for statement in statements
    )
    db.close()


def test_list_leases_filters_one_current_root_from_its_terminal_history(isolated_db):
    from infra.host_access_leases import (
        list_leases,
        record_application_review,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    for _ in range(8):
        requested = request_lease("root", db=db, **_application())
        record_application_review(
            requested["id"],
            expected_revision=requested["revision"],
            application_hash=requested["application_hash"],
            allowed=False,
            reviewer_model="native/reviewer",
            review_result={"reason": "Not bounded."},
            db=db,
        )
    current = request_lease("root", db=db, **_application())

    legacy = list_leases("steve", db=db)
    root_history = list_leases("steve", root_thread_id="root", db=db)
    listed = list_leases(
        "steve",
        root_thread_id="root",
        nonterminal_only=True,
        db=db,
    )

    assert len(legacy) == 9
    assert legacy == list_leases("steve", root_thread_id=None, db=db)
    assert root_history == legacy
    assert listed == [current]
    assert [(row["root_thread_id"], row["state"]) for row in listed] == [
        ("root", "reviewing")
    ]
    db.close()


def test_list_leases_scopes_durable_rows_to_one_root(isolated_db):
    from infra import host_access_lease_reads
    from infra.host_access_leases import record_application_review, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    _session(db, "other-root")

    root_terminal = request_lease("root", db=db, **_application())
    record_application_review(
        root_terminal["id"],
        expected_revision=root_terminal["revision"],
        application_hash=root_terminal["application_hash"],
        allowed=False,
        reviewer_model="native/reviewer",
        review_result={"reason": "Not bounded."},
        db=db,
    )
    root_current = request_lease("root", db=db, **_application())
    other_terminal = request_lease("other-root", db=db, **_application())
    record_application_review(
        other_terminal["id"],
        expected_revision=other_terminal["revision"],
        application_hash=other_terminal["application_hash"],
        allowed=False,
        reviewer_model="native/reviewer",
        review_result={"reason": "Not bounded."},
        db=db,
    )
    other_current = request_lease("other-root", db=db, **_application())

    scoped_history = host_access_lease_reads.list_leases(
        "steve", root_thread_id="root", db=db
    )
    scoped_current = host_access_lease_reads.list_leases(
        "steve",
        root_thread_id="root",
        nonterminal_only=True,
        db=db,
    )
    unfiltered = host_access_lease_reads.list_leases("steve", db=db)

    assert {row["id"] for row in scoped_history} == {
        root_terminal["id"],
        root_current["id"],
    }
    assert [row["id"] for row in scoped_current] == [root_current["id"]]
    assert {row["id"] for row in unfiltered} == {
        root_terminal["id"],
        root_current["id"],
        other_terminal["id"],
        other_current["id"],
    }
    db.close()


def test_list_leases_expires_due_rows_before_returning_the_read_snapshot(isolated_db):
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        get_lease,
        list_leases,
        record_application_review,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "Bounded and reviewable."},
        db=db,
    )
    approved = approve_lease(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )
    active = activate_lease(
        approved["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        now=datetime(2026, 7, 18, 12, 0, 0),
        db=db,
    )

    listed = list_leases("steve", now=datetime(2026, 7, 18, 12, 2, 0), db=db)

    assert listed[0]["id"] == active["id"]
    assert listed[0]["state"] == "expired"
    assert listed == [get_lease(active["id"], db=db)]
    db.close()


def test_request_normalizes_and_hash_binds_active_policy_exceptions(isolated_db):
    from infra.host_access_leases import LeaseConflictError, request_lease

    db = _connection(isolated_db)
    _project(db, "project-a")
    _session(db, "root", project_id="project-a")
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES ('project-a','corsia','/srv/corsia','dev',1)"
    )
    db.commit()

    lease = request_lease(
        "root",
        db=db,
        **_application(
            requested_policy_exceptions=[
                {
                    "repo_name": " corsia ",
                    "policy": "branch_pinning",
                    "reason": " Emergency release repair ",
                },
                {
                    "repo_name": "corsia",
                    "policy": "protected_base_checkout",
                    "reason": "Repair the serving checkout",
                },
            ]
        ),
    )

    assert lease["requested_policy_exceptions"] == [
        {
            "repo_name": "corsia",
            "policy": "branch_pinning",
            "reason": "Emergency release repair",
        },
        {
            "repo_name": "corsia",
            "policy": "protected_base_checkout",
            "reason": "Repair the serving checkout",
        },
    ]
    stored = db.execute(
        "SELECT requested_policy_exceptions FROM host_access_leases "
        "WHERE id=?",
        (lease["id"],),
    ).fetchone()[0]
    assert json.loads(stored) == lease["requested_policy_exceptions"]
    assert len(lease["application_hash"]) == 64
    with pytest.raises(LeaseConflictError):
        request_lease(
            "root",
            db=db,
            **_application(
                requested_policy_exceptions=[
                    {
                        "repo_name": "corsia",
                        "policy": "branch_pinning",
                        "reason": "A materially different reason",
                    }
                ]
            ),
        )
    db.close()


@pytest.mark.parametrize(
    "command",
    [
        f"OPENAI_API_KEY={'a' * 40} codex exec 'Analyze report.txt'",
        f"codex exec --api-key {'a' * 40} 'Analyze report.txt'",
    ],
)
def test_external_agent_exception_rejects_literal_credentials(
    isolated_db, tmp_path, command
):
    from host_access.refusal_messages import LITERAL_CREDENTIAL_REFUSAL
    from infra.host_access_leases import LeaseValidationError, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    cwd = tmp_path / "external"
    cwd.mkdir()

    with pytest.raises(LeaseValidationError, match=re.escape(LITERAL_CREDENTIAL_REFUSAL)):
        request_lease(
            "root",
            db=db,
            **_application(
                requested_policy_exceptions=[
                    {
                        "policy": "external_agent",
                        "reason": "Independent analysis",
                        "command": command,
                        "working_directory": str(cwd),
                    }
                ]
            ),
        )

    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.close()


def test_external_agent_exception_preserves_indirect_credential_reference(
    isolated_db, tmp_path
):
    from infra.host_access_leases import request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    cwd = tmp_path / "external"
    cwd.mkdir()
    command = 'OPENAI_API_KEY="$OPENAI_API_KEY" codex exec "Analyze report.txt"'

    lease = request_lease(
        "root",
        db=db,
        **_application(
            requested_policy_exceptions=[
                {
                    "policy": "external_agent",
                    "reason": "Independent analysis",
                    "command": command,
                    "working_directory": str(cwd),
                }
            ]
        ),
    )

    assert lease["requested_policy_exceptions"][0]["command"] == command
    stored = db.execute(
        "SELECT requested_policy_exceptions FROM host_access_leases"
    ).fetchone()[0]
    assert json.loads(stored)[0]["command"] == command
    db.close()


def test_external_agent_exception_is_projectless_and_hash_bound(isolated_db, tmp_path):
    from infra.host_access_exceptions import external_agent_exception_matches
    from infra.host_access_leases import release_lease, request_lease

    db = _connection(isolated_db)
    _session(db, "root", project_id=None)
    cwd = tmp_path / "external"
    cwd.mkdir()
    linked_cwd = tmp_path / "external-link"
    linked_cwd.symlink_to(cwd, target_is_directory=True)
    changed_cwd = tmp_path / "changed-external"
    changed_cwd.mkdir()
    command = "claude -p 'Analyze report.txt'"
    lease = request_lease(
        "root",
        db=db,
        **_application(
            requested_policy_exceptions=[
                {
                    "policy": "external_agent",
                    "reason": "Independent analysis",
                    "command": command,
                    "working_directory": str(linked_cwd),
                }
            ]
        ),
    )

    assert lease["requested_policy_exceptions"] == [
        {
            "policy": "external_agent",
            "reason": "Independent analysis",
            "command": command,
            "working_directory": str(cwd.resolve()),
        }
    ]
    assert external_agent_exception_matches(
        lease, command=command, working_directory=cwd.resolve()
    )
    assert not external_agent_exception_matches(
        lease, command="claude -p 'Changed report.txt'", working_directory=cwd.resolve()
    )
    assert not external_agent_exception_matches(
        lease, command=command, working_directory=changed_cwd.resolve()
    )

    released = release_lease(
        lease["id"],
        root_thread_id="root",
        expected_revision=lease["revision"],
        application_hash=lease["application_hash"],
        db=db,
    )
    changed = request_lease(
        "root",
        db=db,
        **_application(
            requested_policy_exceptions=[
                {
                    "policy": "external_agent",
                    "reason": "Independent analysis",
                    "command": command,
                    "working_directory": str(changed_cwd),
                }
            ]
        ),
    )
    assert released["state"] == "revoked"
    assert changed["application_hash"] != lease["application_hash"]

    release_lease(
        changed["id"],
        root_thread_id="root",
        expected_revision=changed["revision"],
        application_hash=changed["application_hash"],
        db=db,
    )
    changed_command = request_lease(
        "root",
        db=db,
        **_application(
            requested_policy_exceptions=[
                {
                    "policy": "external_agent",
                    "reason": "Independent analysis",
                    "command": "claude -p 'Changed report.txt'",
                    "working_directory": str(changed_cwd),
                }
            ]
        ),
    )
    assert changed_command["application_hash"] != changed["application_hash"]
    db.close()


def test_external_agent_exception_requires_literal_existing_absolute_directory(
    isolated_db,
):
    from infra.host_access_leases import LeaseValidationError, request_lease

    db = _connection(isolated_db)
    _session(db, "root", project_id=None)
    missing_directory = {
        "policy": "external_agent",
        "reason": "Independent analysis",
        "command": "claude -p hello",
    }
    invalid_directories = ("relative-directory", "~/not-a-literal-absolute-path")

    for directory in invalid_directories:
        with pytest.raises(LeaseValidationError, match="existing absolute directory"):
            request_lease(
                "root",
                db=db,
                **_application(
                    requested_policy_exceptions=[
                        {**missing_directory, "working_directory": directory}
                    ]
                ),
            )
    with pytest.raises(LeaseValidationError, match="exactly policy"):
        request_lease(
            "root",
            db=db,
            **_application(requested_policy_exceptions=[missing_directory]),
        )
    db.close()


@pytest.mark.parametrize(
    ("repo_name", "policy", "expected"),
    [
        ("corsia", "branch_pinning", True),
        ("corsia", "protected_base_checkout", False),
        ("other", "branch_pinning", False),
    ],
)
def test_lease_policy_exception_requires_an_exact_repo_and_policy_match(
    repo_name,
    policy,
    expected,
):
    from infra.host_access_leases import lease_has_policy_exception

    lease = {
        "requested_policy_exceptions": [
            {
                "repo_name": "corsia",
                "policy": "branch_pinning",
                "reason": "Emergency repair",
            }
        ]
    }

    assert (
        lease_has_policy_exception(
            lease,
            repo_name=repo_name,
            policy=policy,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("project_id", "policy_rows", "exceptions", "message"),
    [
        (
            None,
            [],
            [{"repo_name": "corsia", "policy": "branch_pinning", "reason": "x"}],
            "projectless",
        ),
        (
            "project-a",
            [("corsia", "/srv/corsia", None, 1)],
            [{"repo_name": "corsia", "policy": "branch_pinning", "reason": "x"}],
            "not active",
        ),
        (
            "project-a",
            [("corsia", "/srv/corsia", "dev", 0)],
            [
                {
                    "repo_name": "corsia",
                    "policy": "protected_base_checkout",
                    "reason": "x",
                }
            ],
            "not active",
        ),
        (
            "project-a",
            [("corsia", "/srv/corsia", "dev", 1)],
            [
                {"repo_name": "missing", "policy": "branch_pinning", "reason": "x"}
            ],
            "not active",
        ),
        (
            "project-a",
            [("corsia", "/srv/corsia", "dev", 1)],
            [
                {"repo_name": "corsia", "policy": "branch_pinning", "reason": "x"},
                {"repo_name": "corsia", "policy": "branch_pinning", "reason": "y"},
            ],
            "duplicate",
        ),
    ],
)
def test_request_rejects_invalid_or_inactive_policy_exceptions(
    isolated_db,
    project_id,
    policy_rows,
    exceptions,
    message,
):
    from infra.host_access_leases import LeaseValidationError, request_lease

    db = _connection(isolated_db)
    if project_id is not None:
        _project(db, project_id)
        db.executemany(
            "INSERT INTO project_repo_policies "
            "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
            "VALUES (?,?,?,?,?)",
            [(project_id, *row) for row in policy_rows],
        )
    _session(db, "root", project_id=project_id)

    with pytest.raises(LeaseValidationError, match=message):
        request_lease(
            "root",
            db=db,
            **_application(requested_policy_exceptions=exceptions),
        )

    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize(
    "exceptions",
    [
        "not-an-array",
        [{"repo_name": "corsia", "policy": "branch_pinning"}],
        [
            {
                "repo_name": "corsia",
                "policy": "branch_pinning",
                "reason": "x",
                "extra": True,
            }
        ],
        [{"repo_name": "corsia", "policy": "unknown", "reason": "x"}],
        [
            {
                "repo_name": "corsia",
                "policy": "external_agent",
                "reason": "x",
                "command": "claude -p hello",
                "working_directory": "/tmp",
            }
        ],
        [
            {
                "repo_name": "corsia",
                "policy": "branch_pinning",
                "reason": "x" * 1001,
            }
        ],
        [
            {"repo_name": f"repo-{index}", "policy": "branch_pinning", "reason": "x"}
            for index in range(9)
        ],
    ],
)
def test_policy_exception_shape_is_strict_and_bounded(isolated_db, exceptions):
    from infra.host_access_leases import LeaseValidationError, request_lease

    db = _connection(isolated_db)
    _project(db, "project-a")
    _session(db, "root", project_id="project-a")

    with pytest.raises(LeaseValidationError):
        request_lease(
            "root",
            db=db,
            **_application(requested_policy_exceptions=exceptions),
        )
    db.close()


def test_request_snapshots_only_authoritative_project_repo_rows_in_order(
    isolated_db,
):
    from infra.host_access_leases import request_lease

    db = _connection(isolated_db)
    _project(db, "project-a")
    _project(db, "project-b")
    _session(db, "root", project_id="project-a")
    db.executemany(
        "INSERT INTO project_repo_policies "
        "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES (?,?,?,?,?)",
        [
            ("project-a", "zeta", "/srv/zeta", None, 0),
            ("project-b", "other", "/srv/other", "main", 1),
            ("project-a", "alpha", "/srv/alpha", "dev", 1),
        ],
    )
    db.commit()

    lease = request_lease("root", db=db, **_application())

    expected_snapshot = [
        {
            "repo_name": "alpha",
            "repo_path": "/srv/alpha",
            "pinned_branch": "dev",
            "protect_base_checkout": True,
        },
        {
            "repo_name": "zeta",
            "repo_path": "/srv/zeta",
            "pinned_branch": None,
            "protect_base_checkout": False,
        },
    ]
    expected_policy_payload = {
        "requesting_project_id": "project-a",
        "repo_policy_snapshot": expected_snapshot,
        "version": 1,
    }
    expected_policy_hash = hashlib.sha256(
        json.dumps(
            expected_policy_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    expected_application_hash = hashlib.sha256(
        json.dumps(
            {
                "application": {
                    "duration_seconds": 120,
                    "expected_resources": "process table",
                    "forbidden_effects": "no restarts",
                    "persistent_effects": None,
                    "plan": ["Inspect processes", "Read logs"],
                        "purpose": "Diagnose the local runtime",
                        "requested_policy_exceptions": [],
                        "rollback_plan": None,
                },
                "repo_policy_hash": expected_policy_hash,
                "repo_policy_snapshot": expected_snapshot,
                "requesting_project_id": "project-a",
                "requesting_session_id": "root",
                "user_id": "steve",
                    "version": 3,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert lease["requesting_project_id"] == "project-a"
    assert lease["repo_policy_snapshot"] == expected_snapshot
    assert lease["repo_policy_hash"] == expected_policy_hash
    assert lease["application_hash"] == expected_application_hash
    assert "other" not in {row["repo_name"] for row in lease["repo_policy_snapshot"]}
    db.close()


def test_projectless_request_has_explicit_empty_policy_snapshot(isolated_db):
    from infra.host_access_leases import request_lease

    db = _connection(isolated_db)
    _session(db, "root")

    lease = request_lease("root", db=db, **_application())

    assert lease["requesting_project_id"] is None
    assert lease["repo_policy_snapshot"] == []
    assert len(lease["repo_policy_hash"]) == 64
    db.close()


@pytest.mark.parametrize(
    ("repo_name", "repo_path"),
    [
        (".hidden", "/srv/repo"),
        ("repo", "relative/repo"),
    ],
)
def test_request_rejects_malformed_authoritative_repo_policy_rows(
    isolated_db,
    repo_name,
    repo_path,
):
    from infra.host_access_leases import LeaseLineageError, request_lease

    db = _connection(isolated_db)
    _project(db, "project-a")
    _session(db, "root", project_id="project-a")
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES (?,?,?,?,?)",
        ("project-a", repo_name, repo_path, "dev", 1),
    )
    db.commit()

    with pytest.raises(LeaseLineageError, match="repository policy is malformed"):
        request_lease("root", db=db, **_application())

    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.close()


def test_request_fails_closed_when_repo_policy_query_is_unavailable(isolated_db):
    from infra.host_access_leases import LeaseLineageError, request_lease

    db = _connection(isolated_db)
    _project(db, "project-a")
    _session(db, "root", project_id="project-a")
    db.execute("DROP TABLE project_repo_policies")
    db.commit()

    with pytest.raises(LeaseLineageError, match="repository policy is unavailable"):
        request_lease("root", db=db, **_application())

    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.close()


def test_project_policy_identity_is_hash_bound_and_immutable(isolated_db):
    from infra.host_access_leases import LeaseConflictError, request_lease

    db = _connection(isolated_db)
    _project(db, "project-a")
    _project(db, "project-b")
    _session(db, "root", project_id="project-a")
    lease = request_lease("root", db=db, **_application())

    db.execute("UPDATE threads SET project_id='project-b' WHERE id='root'")
    db.commit()
    with pytest.raises(LeaseConflictError):
        request_lease("root", db=db, **_application())

    for column, value in (
        ("requesting_project_id", "project-b"),
        ("repo_policy_snapshot", '[{"repo_name":"tampered"}]'),
        ("repo_policy_hash", "0" * 64),
        ("requested_policy_exceptions", '[{}]'),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                f"UPDATE host_access_leases SET {column}=? WHERE id=?",
                (value, lease["id"]),
            )
        db.rollback()
    db.close()


def test_duration_ceiling_is_thirty_minutes(isolated_db):
    from infra.host_access_leases import LeaseValidationError, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    lease = request_lease(
        "root",
        db=db,
        **_application(duration_seconds=1800),
    )

    assert lease["requested_duration_seconds"] == 1800
    with pytest.raises(LeaseValidationError, match="1 through 1800"):
        request_lease(
            "root",
            db=db,
            **_application(duration_seconds=1801),
        )

    db.close()


def test_application_order_and_material_change_conflict_while_open(isolated_db):
    from infra.host_access_leases import LeaseConflictError, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    request_lease("root", db=db, **_application())

    with pytest.raises(LeaseConflictError):
        request_lease(
            "root",
            db=db,
            **_application(plan=["Read logs", "Inspect processes"]),
        )
    db.close()


def test_schema_prevents_application_or_ownership_mutation(isolated_db):
    from infra.host_access_leases import request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    lease = request_lease("root", db=db, **_application())

    for column, value in (
        ("purpose", "different"),
        ("root_thread_id", "other"),
        ("user_id", "leksi"),
        ("requested_duration_seconds", 10),
        ("application_hash", "0" * 64),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"UPDATE host_access_leases SET {column}=? WHERE id=?",
                (value, lease["id"]),
            )
        db.rollback()
    db.close()


def test_decisions_use_hash_revision_cas_and_do_not_reopen(isolated_db):
    from infra.host_access_leases import (
        LeaseConflictError,
        activate_lease,
        approve_lease,
        record_activation_wake_delivery,
        record_application_review,
        reject_lease,
        request_lease,
        revoke_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "Bounded and reviewable."},
        db=db,
    )
    assert reviewed["state"] == "awaiting_user"

    approved = approve_lease(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )
    assert approved["state"] == "activating"
    assert approve_lease(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )["revision"] == approved["revision"]

    with pytest.raises(LeaseConflictError):
        reject_lease(
            reviewed["id"],
            user_id="steve",
            expected_revision=approved["revision"],
            application_hash=approved["application_hash"],
            db=db,
        )

    activated = activate_lease(
        approved["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        now=datetime(2026, 7, 18, 12, 0, 0),
        db=db,
    )
    assert activated["state"] == "active"
    assert activated["expires_at"] == "2026-07-18T12:02:00"
    assert not activated["expires_at"].endswith("Z")
    assert activated["activation_wake_delivered_at"] is None

    delivered = record_activation_wake_delivery(
        activated["id"],
        expected_revision=activated["revision"],
        application_hash=activated["application_hash"],
        now=datetime(2026, 7, 18, 12, 0, 0, 500000),
        db=db,
    )
    assert delivered["activation_wake_delivered_at"] == (
        "2026-07-18T12:00:00.500000"
    )
    assert delivered["revision"] == activated["revision"]
    assert record_activation_wake_delivery(
        activated["id"],
        expected_revision=activated["revision"],
        application_hash=activated["application_hash"],
        now=datetime(2026, 7, 18, 12, 0, 1),
        db=db,
    )["activation_wake_delivered_at"] == "2026-07-18T12:00:00.500000"

    revoked = revoke_lease(
        delivered["id"],
        user_id="steve",
        expected_revision=delivered["revision"],
        application_hash=delivered["application_hash"],
        now=datetime(2026, 7, 18, 12, 0, 1),
        db=db,
    )
    assert revoked["state"] == "revoked"
    assert revoke_lease(
        activated["id"],
        user_id="steve",
        expected_revision=activated["revision"],
        application_hash=activated["application_hash"],
        db=db,
    )["revision"] == revoked["revision"]
    db.close()


def test_automatic_review_cannot_be_retroactively_attributed_to_a_user(isolated_db):
    from infra.host_access_leases import (
        LeaseConflictError,
        approve_lease,
        record_application_review,
        reject_lease,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        auto_approve=True,
        reviewer_model="native/reviewer",
        review_result={
            "attestation": "Safe to activate.",
            "approval_mode": "manual",
        },
        db=db,
    )

    assert reviewed["state"] == "activating"
    assert reviewed["user_decision"] is None
    assert reviewed["application_review_result"]["approval_mode"] == "automatic"
    for decision in (approve_lease, reject_lease):
        with pytest.raises(LeaseConflictError):
            decision(
                reviewed["id"],
                user_id="steve",
                expected_revision=reviewed["revision"],
                application_hash=reviewed["application_hash"],
                db=db,
            )
    db.close()


def test_release_ends_the_lease_and_frees_the_thread_for_a_new_scope(isolated_db):
    from infra.host_access_leases import (
        LeaseConflictError,
        get_open_lease_for_session,
        record_application_review,
        release_lease,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "Bounded and reviewable."},
        db=db,
    )
    assert reviewed["state"] == "awaiting_user"
    assert get_open_lease_for_session("root", db=db)["id"] == reviewed["id"]

    wider = _application(purpose="Diagnose the local runtime and restart it")
    with pytest.raises(LeaseConflictError, match="release_host_access"):
        request_lease("root", db=db, **wider)

    released = release_lease(
        reviewed["id"],
        root_thread_id="root",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )
    assert released["state"] == "revoked"
    assert released["terminal_reason"] == "agent_released"
    assert released["user_decision"] is None
    assert get_open_lease_for_session("root", db=db) is None

    replacement = request_lease("root", db=db, **wider)
    assert replacement["state"] == "reviewing"
    assert replacement["id"] != reviewed["id"]
    assert release_lease(
        reviewed["id"],
        root_thread_id="root",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )["revision"] == released["revision"]
    db.close()


def test_release_is_bound_to_the_owning_thread_and_live_states(isolated_db):
    from infra.host_access_leases import (
        LeaseConflictError,
        LeaseNotFoundError,
        get_open_lease_for_session,
        release_lease,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    _session(db, "other")
    requested = request_lease("root", db=db, **_application())

    with pytest.raises(LeaseNotFoundError):
        release_lease(
            requested["id"],
            root_thread_id="other",
            expected_revision=requested["revision"],
            application_hash=requested["application_hash"],
            db=db,
        )
    assert get_open_lease_for_session("other", db=db) is None
    assert get_open_lease_for_session("root", db=db)["state"] == "reviewing"

    released = release_lease(
        requested["id"],
        root_thread_id="root",
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        db=db,
    )
    assert released["state"] == "revoked"

    with pytest.raises(LeaseConflictError):
        release_lease(
            requested["id"],
            root_thread_id="root",
            expected_revision=released["revision"],
            application_hash="0" * 64,
            db=db,
        )
    db.close()


def test_domain_rejects_nested_deferred_transaction(isolated_db):
    from infra.host_access_leases import LeaseConflictError, request_lease

    db = _connection(isolated_db)
    _session(db, "root")
    db.execute("BEGIN")
    db.execute("UPDATE threads SET title='pending' WHERE id='root'")

    with pytest.raises(LeaseConflictError, match="BEGIN IMMEDIATE"):
        request_lease("root", db=db, **_application())

    assert db.in_transaction
    assert db.execute(
        "SELECT title FROM threads WHERE id='root'"
    ).fetchone()[0] == "pending"
    db.rollback()
    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.close()


def test_domain_rejects_dirty_pooled_connection_without_rollback(isolated_db):
    from infra.database import get_db
    from infra.host_access_leases import LeaseConflictError, request_lease

    db = get_db()
    _session(db, "root")
    db.execute("UPDATE threads SET title='pending' WHERE id='root'")

    with pytest.raises(LeaseConflictError, match="BEGIN IMMEDIATE"):
        request_lease("root", **_application())

    assert db.in_transaction
    assert db.execute(
        "SELECT title FROM threads WHERE id='root'"
    ).fetchone()[0] == "pending"
    assert db.execute("SELECT count(*) FROM host_access_leases").fetchone()[0] == 0
    db.rollback()
    db.close()


def test_activation_blocks_when_root_identity_disappears(isolated_db):
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        record_application_review,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "ok"},
        db=db,
    )
    approved = approve_lease(
        requested["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM threads WHERE id='root'")
    db.commit()

    blocked = activate_lease(
        requested["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        db=db,
    )

    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "root_identity_unavailable"
    assert blocked["activated_at"] is None
    assert blocked["expires_at"] is None
    db.close()


def test_activation_retry_blocks_active_lease_after_root_identity_disappears(
    isolated_db,
):
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        record_application_review,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "root")
    requested = request_lease("root", db=db, **_application())
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "ok"},
        db=db,
    )
    approved = approve_lease(
        requested["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        db=db,
    )
    active = activate_lease(
        requested["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        db=db,
    )
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM threads WHERE id='root'")
    db.commit()

    blocked = activate_lease(
        requested["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        db=db,
    )

    assert active["state"] == "active"
    assert blocked["state"] == "blocked"
    assert blocked["revision"] == active["revision"] + 1
    assert blocked["terminal_reason"] == "root_identity_unavailable"
    db.close()


def test_review_deny_and_runtime_block_are_terminal(isolated_db):
    from infra.host_access_leases import (
        LeaseConflictError,
        block_lease,
        record_application_review,
        request_lease,
    )

    db = _connection(isolated_db)
    _session(db, "denied-root")
    denied_request = request_lease("denied-root", db=db, **_application())
    denied = record_application_review(
        denied_request["id"],
        expected_revision=denied_request["revision"],
        application_hash=denied_request["application_hash"],
        allowed=False,
        reviewer_model="native/reviewer",
        review_result={"reason": "Not bounded."},
        db=db,
    )
    assert denied["state"] == "rejected"
    with pytest.raises(LeaseConflictError):
        block_lease(
            denied["id"],
            expected_revision=denied["revision"],
            application_hash=denied["application_hash"],
            reason="must not reopen",
            db=db,
        )

    _session(db, "blocked-root")
    blocked_request = request_lease("blocked-root", db=db, **_application())
    blocked = block_lease(
        blocked_request["id"],
        expected_revision=blocked_request["revision"],
        application_hash=blocked_request["application_hash"],
        reason="reviewer unavailable",
        db=db,
    )
    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "reviewer unavailable"
    assert block_lease(
        blocked["id"],
        expected_revision=blocked_request["revision"],
        application_hash=blocked_request["application_hash"],
        reason="reviewer unavailable",
        db=db,
    )["revision"] == blocked["revision"]
    db.close()


def test_concurrent_same_request_is_one_row_and_changed_request_conflicts(isolated_db):
    from infra.host_access_leases import LeaseConflictError, request_lease

    seed = _connection(isolated_db)
    _session(seed, "root")
    seed.close()
    barrier = threading.Barrier(2)
    ids: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        db = _connection(isolated_db)
        try:
            barrier.wait(timeout=2)
            ids.append(request_lease("root", db=db, **_application())["id"])
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(ids) == 2
    assert len(set(ids)) == 1

    db = _connection(isolated_db)
    with pytest.raises(LeaseConflictError):
        request_lease("root", db=db, **_application(purpose="Different work"))
    db.close()
