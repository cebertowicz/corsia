from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest


TARGET = SimpleNamespace(
    catalog_id="native/reviewer",
    harness="native",
    transport_model="openai/reviewer",
    expected_reported_model="openai/reviewer",
    reported_model="openai/reviewer",
    display_name="Reviewer",
)
IDENTITY = SimpleNamespace(
    catalog_id=TARGET.catalog_id,
    harness=TARGET.harness,
    transport_model=TARGET.transport_model,
    reported_model=TARGET.reported_model,
    display_name=TARGET.display_name,
)


def _application_review(verdict: str = "allow"):
    return SimpleNamespace(
        identity=IDENTITY,
        schema_version=3,
        verdict=verdict,
        reason_category="not_applicable" if verdict == "allow" else "unclear_actions",
        reason="The request is bounded.",
        user_attestation="This request matches the stated plan.",
        reviewer_fallback=False,
        allowed=verdict == "allow",
    )


def _action_review(verdict: str = "allow"):
    return SimpleNamespace(
        identity=IDENTITY,
        schema_version=1,
        verdict=verdict,
        reason="The action matches the approved plan.",
        allowed=verdict == "allow",
    )


def _bash_descriptor() -> dict:
    return {
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
    }


def _seed_session(
    isolated_db,
    thread_id: str,
    *,
    spawned_by=None,
    project_id: str | None = None,
) -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,spawned_by,project_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (thread_id, "steve", thread_id, spawned_by, project_id, "t", "t"),
    )
    db.commit()
    db.close()


def test_host_access_change_nudge_names_the_visible_root_for_a_child_lease(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra import state

    _seed_session(isolated_db, "visible-root")
    _seed_session(isolated_db, "child", spawned_by="visible-root")
    broadcasts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        state,
        "broadcast_to_user",
        lambda user_id, payload: broadcasts.append((user_id, payload)),
    )

    supervisor._notify_changed({
        "id": "lease-1",
        "user_id": "steve",
        "root_thread_id": "child",
    })

    assert broadcasts == [("steve", {
        "type": "host_access_changed",
        "lease_id": "lease-1",
        "thread_ids": ["child", "visible-root"],
    })]


def test_host_access_change_nudge_names_no_threads_when_a_hidden_childs_lookup_fails(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra import database, state

    _seed_session(isolated_db, "visible-root")
    _seed_session(isolated_db, "child", spawned_by="visible-root")
    broadcasts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        state,
        "broadcast_to_user",
        lambda user_id, payload: broadcasts.append((user_id, payload)),
    )

    def fail_lookup():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "get_db", fail_lookup)

    supervisor._notify_changed({
        "id": "lease-1",
        "user_id": "steve",
        "root_thread_id": "child",
    })

    assert broadcasts == [("steve", {
        "type": "host_access_changed",
        "lease_id": "lease-1",
    })]


@pytest.mark.asyncio
async def test_broadcast_failure_still_cleans_then_rethrows_from_emit_terminal(
    monkeypatch,
) -> None:
    from host_access import change_nudge, supervisor
    from infra import state

    order: list[str] = []

    def fail_broadcast(_user_id, _payload):
        order.append("broadcast")
        raise RuntimeError("broadcast failed")

    async def cleanup(lease) -> None:
        order.append(f"cleanup:{lease['id']}")

    monkeypatch.setattr(state, "broadcast_to_user", fail_broadcast)
    monkeypatch.setattr(change_nudge, "root_visible_thread_ids", lambda ids: {})
    supervisor.configure_host_access_terminal_callback(cleanup)
    try:
        with pytest.raises(RuntimeError, match="broadcast failed"):
            await supervisor._emit_terminal({
                "id": "lease-1",
                "user_id": "steve",
                "root_thread_id": "root",
            })
    finally:
        supervisor.configure_host_access_terminal_callback(None)

    assert order == ["broadcast", "cleanup:lease-1"]


def _configure_review_fakes(monkeypatch, supervisor, *, application="allow"):
    async def resolve(expected_catalog_id=None, **_kwargs):
        if expected_catalog_id is not None:
            assert expected_catalog_id == TARGET.catalog_id
        return TARGET

    async def review_application(_target, _payload, *, auto_approve=False):
        return _application_review(application)

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(
        supervisor,
        "_invoke_application_review",
        review_application,
    )


async def _reviewed_request(monkeypatch, supervisor, **overrides):
    _configure_review_fakes(monkeypatch, supervisor)
    return await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
        **overrides,
    )


def _seed_project_policy(
    isolated_db,
    *,
    pinned_branch: str | None = "dev",
    protect_base_checkout: bool = True,
) -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-a','Project A','t','t')"
    )
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES ('project-a','corsia','/srv/corsia',?,?)",
        (pinned_branch, protect_base_checkout),
    )
    db.commit()
    db.close()


async def _active_project_lease(
    isolated_db,
    monkeypatch,
    supervisor,
    *,
    pinned_branch: str | None = "dev",
    protect_base_checkout: bool = False,
    requested_policy_exceptions: list[dict[str, str]] | None = None,
) -> dict:
    _seed_project_policy(
        isolated_db,
        pinned_branch=pinned_branch,
        protect_base_checkout=protect_base_checkout,
    )
    _seed_session(isolated_db, "root", project_id="project-a")
    _seed_session(isolated_db, "child", spawned_by="root", project_id="project-a")
    reviewed = await _reviewed_request(
        monkeypatch,
        supervisor,
        requested_policy_exceptions=requested_policy_exceptions or [],
    )
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    return await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )


@pytest.mark.asyncio
async def test_application_reviewer_receives_hash_bound_repo_context_and_exceptions(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")
    captured = {}

    async def resolve(_expected_catalog_id=None, **_kwargs):
        return TARGET

    async def review(_target, payload, *, auto_approve=False):
        captured.update(payload)
        return _application_review()

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(supervisor, "_invoke_application_review", review)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Repair the serving checkout",
        plan=["Apply the reviewed repair"],
        duration_seconds=60,
        requested_policy_exceptions=[
            {
                "repo_name": "corsia",
                "policy": "branch_pinning",
                "reason": "The emergency repair must land on main",
            }
        ],
    )

    assert lease["state"] == "awaiting_user"
    assert captured["requesting_project_id"] == "project-a"
    assert captured["repo_policy_hash"] == lease["repo_policy_hash"]
    assert captured["repo_policy_snapshot"] == lease["repo_policy_snapshot"]
    assert captured["requested_policy_exceptions"] == (
        lease["requested_policy_exceptions"]
    )


@pytest.mark.asyncio
async def test_auto_approval_activates_without_claiming_a_user_decision(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.settings import save_agent_defaults

    _seed_session(isolated_db, "root")
    save_agent_defaults({"host_access_auto_approve": True})
    captured = {}

    async def review(_target, _payload, *, auto_approve=False):
        captured["auto_approve"] = auto_approve
        return _application_review()

    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "_invoke_application_review", review)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )
    wake_calls = []

    async def wake(lease):
        wake_calls.append(lease["id"])
        return True

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert captured["auto_approve"] is True
    assert lease["state"] == "active"
    assert lease["user_decision"] is None
    assert lease["application_review_result"]["approval_mode"] == "automatic"
    assert lease["activated_at"] is not None
    assert wake_calls == []

    revoked = await supervisor.revoke_active_lease(
        lease["id"],
        user_id="steve",
        expected_revision=lease["revision"],
        application_hash=lease["application_hash"],
    )
    assert revoked["state"] == "revoked"
    assert revoked["terminal_reason"] == "user_revoked"


@pytest.mark.asyncio
async def test_disabling_auto_approval_during_review_falls_back_to_user_decision(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.settings import save_agent_defaults

    _seed_session(isolated_db, "root")
    save_agent_defaults({"host_access_auto_approve": True})
    captured = {}

    async def review(_target, _payload, *, auto_approve=False):
        captured["auto_approve"] = auto_approve
        save_agent_defaults({"host_access_auto_approve": False})
        return _application_review()

    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "_invoke_application_review", review)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert captured["auto_approve"] is True
    assert lease["state"] == "awaiting_user"
    assert lease["application_review_result"]["approval_mode"] == "manual"
    assert lease["user_decision"] is None


@pytest.mark.asyncio
async def test_policy_exceptions_always_require_a_user_decision(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.settings import save_agent_defaults

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")
    save_agent_defaults({"host_access_auto_approve": True})
    captured = {}

    async def review(_target, _payload, *, auto_approve=False):
        captured["auto_approve"] = auto_approve
        return _application_review()

    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "_invoke_application_review", review)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Repair the serving checkout",
        plan=["Apply the reviewed repair"],
        duration_seconds=60,
        requested_policy_exceptions=[
            {
                "repo_name": "corsia",
                "policy": "branch_pinning",
                "reason": "The emergency repair must land on main",
            }
        ],
    )

    assert captured["auto_approve"] is False
    assert lease["state"] == "awaiting_user"
    assert lease["application_review_result"]["approval_mode"] == "manual"


@pytest.mark.asyncio
async def test_external_agent_exception_always_requires_a_user_decision(
    isolated_db,
    tmp_path,
    monkeypatch,
):
    from host_access import supervisor
    from infra.settings import save_agent_defaults

    _seed_session(isolated_db, "root")
    save_agent_defaults({"host_access_auto_approve": True})
    captured = {}

    async def review(_target, _payload, *, auto_approve=False):
        captured["auto_approve"] = auto_approve
        return _application_review()

    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "_invoke_application_review", review)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Run a reviewed external analysis",
        plan=["Run the immutable external launch"],
        duration_seconds=60,
        requested_policy_exceptions=[
            {
                "policy": "external_agent",
                "reason": "Independent analysis",
                "command": "claude -p 'Analyze report.txt'",
                "working_directory": str(tmp_path),
            }
        ],
    )

    assert captured["auto_approve"] is False
    assert lease["state"] == "awaiting_user"
    assert lease["application_review_result"]["approval_mode"] == "manual"


@pytest.mark.asyncio
async def test_auto_approval_reject_and_activation_failure_remain_fail_closed(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.settings import save_agent_defaults

    _seed_session(isolated_db, "root")
    save_agent_defaults({"host_access_auto_approve": True})
    _configure_review_fakes(monkeypatch, supervisor, application="reject")

    rejected = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert rejected["state"] == "rejected"
    assert rejected["user_decision"] is None
    assert rejected["application_review_result"]["reason_category"] == "unclear_actions"
    assert rejected["application_review_result"]["approval_mode"] == "automatic"

    _seed_session(isolated_db, "second-root")
    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )

    async def fail_install(*_args, **_kwargs):
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(supervisor, "_install_owner", fail_install)
    blocked = await supervisor.request_reviewed_lease(
        "second-root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "activation_failed"


@pytest.mark.asyncio
async def test_release_ends_live_authority_and_allows_a_new_scope(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.db_async import run_in_db
    from infra.host_access_leases import get_open_lease_for_session

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    assert active["state"] == "active"
    assert supervisor.supervisor_is_healthy(active["id"])
    cleaned: list[str] = []
    supervisor.configure_host_access_terminal_callback(
        lambda lease: cleaned.append(lease["id"])
    )

    try:
        released = await supervisor.release_requested_lease("root")
    finally:
        supervisor.configure_host_access_terminal_callback(None)

    assert released["state"] == "revoked"
    assert released["terminal_reason"] == "agent_released"
    # Releasing ends access; it never rewrites who approved the lease.
    assert released["user_decision"] == "approved"
    assert cleaned == [active["id"]]
    assert not supervisor.supervisor_is_healthy(active["id"])
    assert await supervisor.resolve_active_host_access_lease("root") is None
    assert await run_in_db(lambda: get_open_lease_for_session("root")) is None

    replacement = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state and restart the test service",
        plan=["Read process state", "Restart the test service"],
        duration_seconds=60,
    )
    assert replacement["id"] != active["id"]
    assert replacement["state"] == "awaiting_user"


@pytest.mark.asyncio
async def test_release_without_an_open_lease_reports_nothing_to_release(
    isolated_db,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")

    assert await supervisor.release_requested_lease("root") is None


@pytest.mark.asyncio
async def test_release_cannot_end_another_threads_lease(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.db_async import run_in_db
    from infra.host_access_leases import get_open_lease_for_session

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "other")
    reviewed = await _reviewed_request(monkeypatch, supervisor)

    assert await supervisor.release_requested_lease("other") is None
    still_open = await run_in_db(lambda: get_open_lease_for_session("root"))
    assert still_open["id"] == reviewed["id"]
    assert still_open["state"] == "awaiting_user"


@pytest.mark.asyncio
async def test_policy_change_after_application_review_blocks_before_user_prompt(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")

    async def resolve(_expected_catalog_id=None, **_kwargs):
        return TARGET

    async def review(_target, _payload, *, auto_approve=False):
        db = get_db()
        db.execute(
            "UPDATE project_repo_policies SET pinned_branch='main' "
            "WHERE project_id='project-a' AND repo_name='corsia'"
        )
        db.commit()
        db.close()
        return _application_review()

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(supervisor, "_invoke_application_review", review)
    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        lambda _user_id: asyncio.sleep(0),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert lease["state"] == "blocked"
    assert lease["terminal_reason"] == "project_policy_changed"


@pytest.mark.asyncio
async def test_policy_change_at_user_approval_blocks_and_requires_new_request(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    db = get_db()
    db.execute(
        "UPDATE project_repo_policies SET protect_base_checkout=0 "
        "WHERE project_id='project-a' AND repo_name='corsia'"
    )
    db.commit()
    db.close()

    blocked = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "project_policy_changed"


@pytest.mark.asyncio
async def test_policy_lookup_failure_aborts_approval_without_claiming_change(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra import project_repo_policy
    from infra.host_access_leases import get_lease

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")
    reviewed = await _reviewed_request(monkeypatch, supervisor)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("project policy lookup unavailable")

    monkeypatch.setattr(
        project_repo_policy,
        "load_project_repo_policy_snapshot",
        unavailable,
    )

    with pytest.raises(RuntimeError, match="project policy lookup unavailable"):
        await supervisor.approve_and_activate(
            reviewed["id"],
            user_id="steve",
            expected_revision=reviewed["revision"],
            application_hash=reviewed["application_hash"],
        )

    unchanged = get_lease(reviewed["id"])
    assert unchanged["state"] == "awaiting_user"
    assert unchanged["terminal_reason"] is None


@pytest.mark.asyncio
async def test_policy_change_before_action_review_blocks_without_calling_reviewer(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db
    from infra.host_access_leases import get_lease

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")
    _seed_session(isolated_db, "child", spawned_by="root", project_id="project-a")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    db = get_db()
    db.execute(
        "UPDATE project_repo_policies SET pinned_branch='main' "
        "WHERE project_id='project-a' AND repo_name='corsia'"
    )
    db.commit()
    db.close()
    reviewer_called = False

    async def unexpected(*_args):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", unexpected)
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )

    assert reviewer_called is False
    assert get_lease(active["id"])["terminal_reason"] == "project_policy_changed"


@pytest.mark.asyncio
async def test_missing_policy_exception_blocks_base_checkout_before_reviewer(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    active = await _active_project_lease(isolated_db, monkeypatch, supervisor)
    policy = SimpleNamespace(
        repo_name="corsia",
        path=Path("/srv/corsia"),
        branch_pinned=True,
        protect_base_checkout=False,
    )
    monkeypatch.setattr(
        supervisor,
        "registered_repo_for_path",
        lambda _path: policy,
    )
    monkeypatch.setattr(supervisor, "repo_worktree_root", lambda *_args: None)
    reviewer_called = False

    async def unexpected(*_args):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", unexpected)
    descriptor = _bash_descriptor()
    descriptor["working_directory"] = "/srv/corsia/backend"
    try:
        with pytest.raises(supervisor.HostAccessSupervisorError) as error:
            await supervisor.review_host_action(
                active["id"],
                thread_id="child",
                tool_name="bash",
                action_hash="a" * 64,
                redacted_descriptor=descriptor,
            )

        assert str(error.value) == (
            "Blocked: this action targets 'corsia', which is branch-pinned, "
            "and the active lease has no matching policy exception. Work in a "
            "`.worktrees/` worktree instead, or submit a new lease application "
            "with a `branch_pinning` exception for 'corsia'."
        )
        assert reviewer_called is False
        assert get_lease(active["id"])["state"] == "active"
    finally:
        await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_missing_protected_base_exception_blocks_before_reviewer(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    active = await _active_project_lease(
        isolated_db,
        monkeypatch,
        supervisor,
        pinned_branch=None,
        protect_base_checkout=True,
    )
    policy = SimpleNamespace(
        repo_name="corsia",
        path=Path("/srv/corsia"),
        branch_pinned=False,
        protect_base_checkout=True,
    )
    monkeypatch.setattr(
        supervisor,
        "registered_repo_for_path",
        lambda _path: policy,
    )
    monkeypatch.setattr(supervisor, "repo_worktree_root", lambda *_args: None)
    reviewer_called = False

    async def unexpected(*_args):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", unexpected)
    descriptor = _bash_descriptor()
    descriptor["working_directory"] = "/srv/corsia/backend"
    try:
        with pytest.raises(supervisor.HostAccessSupervisorError) as error:
            await supervisor.review_host_action(
                active["id"],
                thread_id="child",
                tool_name="bash",
                action_hash="a" * 64,
                redacted_descriptor=descriptor,
            )

        assert str(error.value) == (
            "Blocked: this action targets 'corsia', which is base-protected, "
            "and the active lease has no matching policy exception. Work in a "
            "`.worktrees/` worktree instead, or submit a new lease application "
            "with a `protected_base_checkout` exception for 'corsia'."
        )
        assert reviewer_called is False
    finally:
        await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_matching_policy_exception_reaches_reviewer(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    active = await _active_project_lease(
        isolated_db,
        monkeypatch,
        supervisor,
        requested_policy_exceptions=[
            {
                "repo_name": "corsia",
                "policy": "branch_pinning",
                "reason": "The approved repair targets the base checkout",
            }
        ],
    )
    policy = SimpleNamespace(
        repo_name="corsia",
        path=Path("/srv/corsia"),
        branch_pinned=True,
        protect_base_checkout=False,
    )
    monkeypatch.setattr(
        supervisor,
        "registered_repo_for_path",
        lambda _path: policy,
    )
    monkeypatch.setattr(supervisor, "repo_worktree_root", lambda *_args: None)
    reviewer_called = False

    async def review_action(*_args):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", review_action)
    descriptor = _bash_descriptor()
    descriptor["working_directory"] = "/srv/corsia/backend"
    try:
        result = await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=descriptor,
        )

        assert reviewer_called is True
        assert result["review"]["verdict"] == "allow"
    finally:
        await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_worktree_target_bypasses_policy_exception_screen(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    active = await _active_project_lease(isolated_db, monkeypatch, supervisor)
    policy = SimpleNamespace(
        repo_name="corsia",
        path=Path("/srv/corsia"),
        branch_pinned=True,
        protect_base_checkout=True,
    )
    monkeypatch.setattr(
        supervisor,
        "registered_repo_for_path",
        lambda _path: policy,
    )
    monkeypatch.setattr(
        supervisor,
        "repo_worktree_root",
        lambda *_args: Path("/srv/corsia/.worktrees/lane-b"),
    )
    reviewer_called = False

    async def review_action(*_args):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", review_action)
    descriptor = _bash_descriptor()
    descriptor["working_directory"] = "/srv/corsia/.worktrees/lane-b/backend"
    try:
        result = await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=descriptor,
        )

        assert reviewer_called is True
        assert result["review"]["verdict"] == "allow"
    finally:
        await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_unresolvable_repo_containment_falls_through_to_reviewer(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    active = await _active_project_lease(isolated_db, monkeypatch, supervisor)

    def unresolved(_path):
        raise OSError("repository registry is unavailable")

    monkeypatch.setattr(supervisor, "registered_repo_for_path", unresolved)
    reviewer_called = False

    async def review_action(*_args):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", review_action)
    descriptor = _bash_descriptor()
    descriptor["working_directory"] = "/srv/corsia/backend"
    try:
        result = await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=descriptor,
        )

        assert reviewer_called is True
        assert result["review"]["verdict"] == "allow"
    finally:
        await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_policy_change_during_action_review_blocks_without_recording_permit(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db
    from infra.host_access_leases import get_lease

    _seed_project_policy(isolated_db)
    _seed_session(isolated_db, "root", project_id="project-a")
    _seed_session(isolated_db, "child", spawned_by="root", project_id="project-a")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    async def review_action(_target, _application, _action):
        db = get_db()
        db.execute(
            "UPDATE project_repo_policies SET pinned_branch='main' "
            "WHERE project_id='project-a' AND repo_name='corsia'"
        )
        db.commit()
        db.close()
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", review_action)
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )

    db = get_db()
    recorded_reviews = db.execute(
        "SELECT COUNT(*) FROM host_access_action_reviews WHERE lease_id=?",
        (active["id"],),
    ).fetchone()[0]
    db.close()
    assert recorded_reviews == 0
    lease = get_lease(active["id"])
    assert lease["state"] == "blocked"
    assert lease["terminal_reason"] == "project_policy_changed"


@pytest.mark.asyncio
async def test_terminal_callback_failure_is_logged_and_propagated(
    monkeypatch,
) -> None:
    from host_access import supervisor
    from loguru import logger

    logged: list[tuple[object, ...]] = []

    async def fail_cleanup(_lease) -> None:
        raise RuntimeError("process death unverified")

    monkeypatch.setattr(supervisor, "_notify_changed", lambda _lease: None)
    monkeypatch.setattr(
        logger,
        "error",
        lambda *args, **_kwargs: logged.append(args),
    )
    supervisor.configure_host_access_terminal_callback(fail_cleanup)
    try:
        with pytest.raises(RuntimeError, match="process death unverified"):
            await supervisor._emit_terminal(
                {
                    "id": "lease-a",
                    "user_id": "steve",
                    "state": "revoked",
                }
            )
    finally:
        supervisor.configure_host_access_terminal_callback(None)

    assert logged
    assert logged[0][0] == "host-access terminal cleanup failed for {}: {}"
    assert logged[0][1] == "lease-a"
    assert isinstance(logged[0][2], RuntimeError)


@pytest.mark.asyncio
async def test_owner_expiry_projection_failure_still_cleans_after_fencing(
    monkeypatch,
) -> None:
    from host_access import supervisor

    loop = asyncio.get_running_loop()
    owner = supervisor._LeaseOwner(
        lease_id="lease-db-failure",
        application_hash="a" * 64,
        reviewer_model=TARGET.catalog_id,
        reviewer_binding=supervisor.reviewer_target.target_binding(TARGET),
        ready=loop.create_future(),
        root_thread_id="root",
        user_id="steve",
        application_json=supervisor._encoded({"root_thread_id": "root"}),
        public_lease_json=supervisor._encoded(
            {
                "id": "lease-db-failure",
                "root_thread_id": "root",
                "user_id": "steve",
                "state": "active",
                "revision": 4,
                "application_hash": "a" * 64,
                "created_at": "t",
                "updated_at": "t",
            }
        ),
        active_revision=4,
        monotonic_deadline=time.monotonic() - 1,
    )
    owner.activated.set()
    supervisor._owners[owner.lease_id] = owner
    cleaned: list[dict] = []

    def fail_database(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    async def cleanup(lease):
        assert supervisor._owners.get(owner.lease_id) is owner
        assert owner.stopping
        assert not supervisor._owner_is_active(owner)
        cleaned.append(lease)

    monkeypatch.setattr(
        supervisor,
        "terminate_lease_projection",
        fail_database,
    )
    supervisor.configure_host_access_terminal_callback(cleanup)
    owner.task = asyncio.create_task(supervisor._run_owner(owner))
    try:
        await owner.task
    finally:
        supervisor.configure_host_access_terminal_callback(None)
        supervisor._owners.pop(owner.lease_id, None)

    assert cleaned[0]["id"] == owner.lease_id
    assert cleaned[0]["state"] == "expired"
    assert cleaned[0]["terminal_reason"] == "lease_expired"
    assert owner.lease_id not in supervisor._owners


@pytest.mark.asyncio
async def test_owner_expiry_cleanup_failure_is_not_retried(
    monkeypatch,
) -> None:
    from host_access import supervisor

    loop = asyncio.get_running_loop()
    owner = supervisor._LeaseOwner(
        lease_id="lease-cleanup-failure",
        application_hash="a" * 64,
        reviewer_model=TARGET.catalog_id,
        reviewer_binding=supervisor.reviewer_target.target_binding(TARGET),
        ready=loop.create_future(),
        root_thread_id="root",
        user_id="steve",
        application_json=supervisor._encoded({"root_thread_id": "root"}),
        public_lease_json=supervisor._encoded(
            {
                "id": "lease-cleanup-failure",
                "root_thread_id": "root",
                "user_id": "steve",
                "state": "active",
                "revision": 4,
                "application_hash": "a" * 64,
                "created_at": "t",
                "updated_at": "t",
            }
        ),
        active_revision=4,
        monotonic_deadline=time.monotonic() - 1,
    )
    owner.activated.set()
    supervisor._owners[owner.lease_id] = owner
    cleanup_calls: list[str] = []

    async def project(_owner, *, state, reason):
        return supervisor._terminal_owner_snapshot(
            _owner,
            state=state,
            reason=reason,
        )

    async def fail_cleanup(lease):
        cleanup_calls.append(lease["id"])
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(supervisor, "_project_owner_terminal", project)
    monkeypatch.setattr(supervisor, "_notify_changed", lambda _lease: None)
    supervisor.configure_host_access_terminal_callback(fail_cleanup)
    owner.task = asyncio.create_task(supervisor._run_owner(owner))
    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await owner.task
    finally:
        supervisor.configure_host_access_terminal_callback(None)
        supervisor._owners.pop(owner.lease_id, None)

    assert cleanup_calls == [owner.lease_id]


@pytest.mark.asyncio
async def test_application_allow_commits_before_user_prompt(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    changed = []
    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_notify_changed",
        lambda lease: changed.append((lease["id"], lease["state"])),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert lease["state"] == "awaiting_user"
    assert lease["reviewer"] == {
        "model": TARGET.catalog_id,
        "display_name": TARGET.display_name,
        "attestation": "This request matches the stated plan.",
    }
    assert lease["application_review_result"]["reviewer_transport_model"] == (
        TARGET.transport_model
    )
    assert changed[-1] == (lease["id"], "awaiting_user")


@pytest.mark.asyncio
async def test_application_allow_closes_user_model_browsers_before_prompt(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "sibling")
    _configure_review_fakes(monkeypatch, supervisor)
    events: list[tuple[str, str]] = []

    async def close_user_browsers(user_id: str) -> None:
        events.append(("close", user_id))

    monkeypatch.setattr(
        supervisor,
        "_close_user_model_browsers",
        close_user_browsers,
        raising=False,
    )
    monkeypatch.setattr(
        supervisor,
        "_notify_changed",
        lambda lease: events.append(("prompt", lease["state"])),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert lease["state"] == "awaiting_user"
    assert events == [("close", "steve"), ("prompt", "awaiting_user")]


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_approve", [False, True])
async def test_browser_isolation_failure_after_allow_records_review_and_stage(
    isolated_db,
    monkeypatch,
    auto_approve,
):
    from host_access import processes, supervisor

    _seed_session(isolated_db, "root")
    _configure_review_fakes(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "get_host_access_auto_approve", lambda: auto_approve)
    changed: list[str] = []

    class Owner:
        async def close_browser(self, _thread_id: str) -> None:
            raise RuntimeError("browser owner unavailable")

    monkeypatch.setattr(processes, "PROCESS_OWNER", Owner())
    monkeypatch.setattr(
        supervisor,
        "_notify_changed",
        lambda lease: changed.append(lease["state"]),
    )

    lease = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert lease["state"] == "blocked"
    assert lease["terminal_reason"] == "browser_isolation_failed"
    assert lease["application_review_result"]["verdict"] == "allow"
    assert changed == ["blocked"]


@pytest.mark.asyncio
async def test_user_browser_shutdown_closes_every_owned_session(
    isolated_db,
    monkeypatch,
):
    from host_access import processes, supervisor
    from infra.database import get_db

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "sibling")
    db = get_db()
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("foreign", "other-user", "foreign", "t", "t"),
    )
    db.commit()
    db.close()
    closed: list[str] = []

    class Owner:
        async def close_browser(self, thread_id: str) -> None:
            closed.append(thread_id)

    monkeypatch.setattr(processes, "PROCESS_OWNER", Owner())

    await supervisor._close_user_model_browsers("steve")

    assert closed == ["root", "sibling"]


@pytest.mark.asyncio
async def test_application_reject_and_reviewer_failure_never_prompt(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "rejected")
    _seed_session(isolated_db, "failed")
    _configure_review_fakes(monkeypatch, supervisor, application="reject")
    rejected = await supervisor.request_reviewed_lease(
        "rejected",
        purpose="Change host state",
        plan=["Make an unbounded change"],
        duration_seconds=60,
    )
    assert rejected["state"] == "rejected"

    async def explode(_target, _payload, *, auto_approve=False):
        raise RuntimeError("reviewer unavailable")

    monkeypatch.setattr(supervisor, "_invoke_application_review", explode)
    failed = await supervisor.request_reviewed_lease(
        "failed",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )
    assert failed["state"] == "blocked"
    # A plain RuntimeError is not a cause this code can establish, so the
    # stage is reported alone rather than asserting a wrong correction.
    assert failed["terminal_reason"] == "application_reviewer_failure"
    assert "RuntimeError" not in failed["terminal_reason"]
    assert "reviewer unavailable" not in failed["terminal_reason"]


@pytest.mark.asyncio
async def test_duplicate_application_runs_exactly_one_review(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    _configure_review_fakes(monkeypatch, supervisor)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def review_once(_target, _payload, *, auto_approve=False):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _application_review()

    monkeypatch.setattr(supervisor, "_invoke_application_review", review_once)
    kwargs = {
        "purpose": "Inspect host state",
        "plan": ["Read process state"],
        "duration_seconds": 60,
    }
    first = asyncio.create_task(
        supervisor.request_reviewed_lease("root", **kwargs)
    )
    await entered.wait()
    second = asyncio.create_task(
        supervisor.request_reviewed_lease("root", **kwargs)
    )
    release.set()
    rows = await asyncio.gather(first, second)

    assert calls == 1
    assert {row["state"] for row in rows} == {"awaiting_user"}


@pytest.mark.asyncio
async def test_approval_installs_supervisor_before_activation_and_wake(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    observed = []

    async def wake(lease):
        observed.append(
            (lease["state"], supervisor.supervisor_is_healthy(lease["id"]))
        )
        return True

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert active["state"] == "active"
    assert active["activation_wake_delivered_at"] is not None
    assert observed == [("active", True)]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_activation_wake_failure_keeps_lease_active_and_armed(
    isolated_db,
    monkeypatch,
):
    """Regression for the 7 activation_wake_failed leases (e.g. 2a346c92).

    The wake is a courtesy notification; a busy requesting thread rejecting
    it must not terminalize the freshly armed lease. The lease stays active
    and armed, no wake receipt is fabricated, the failure is logged loudly,
    and after the single retry also fails the lease is still alive.
    """
    from host_access import supervisor
    from infra.host_access_leases import get_lease
    from loguru import logger

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor, "_ACTIVATION_WAKE_RETRY_DELAY_SECONDS", 0.0
    )
    errors: list[str] = []
    monkeypatch.setattr(
        logger,
        "error",
        lambda message, *args, **_kwargs: errors.append(
            message.format(*args) if args else message
        ),
    )

    async def wake(_lease):
        return False

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert active["state"] == "active"
    assert active["terminal_reason"] is None
    assert active["activation_wake_delivered_at"] is None
    assert supervisor.host_access_supervisor_count() == 1
    assert supervisor.supervisor_is_healthy(active["id"])
    assert await supervisor.resolve_active_host_access_lease("root") is not None
    assert any(
        active["id"] in message and "stays active and armed" in message
        for message in errors
    )

    owner = supervisor._owners[active["id"]]
    assert owner.wake_retry_task is not None
    await owner.wake_retry_task

    assert any("giving up" in message for message in errors)
    persisted = get_lease(active["id"])
    assert persisted["state"] == "active"
    assert persisted["terminal_reason"] is None
    assert persisted["activation_wake_delivered_at"] is None
    assert supervisor.supervisor_is_healthy(active["id"])
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_activation_wake_retry_success_records_delivery(
    isolated_db,
    monkeypatch,
):
    """A successful wake retry records the receipt on the live lease."""
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor, "_ACTIVATION_WAKE_RETRY_DELAY_SECONDS", 0.0
    )
    attempts: list[str] = []

    async def wake(lease):
        attempts.append(lease["id"])
        return len(attempts) > 1

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert active["state"] == "active"
    assert active["activation_wake_delivered_at"] is None
    owner = supervisor._owners[active["id"]]
    assert owner.wake_retry_task is not None
    await owner.wake_retry_task

    assert attempts == [active["id"], active["id"]]
    persisted = get_lease(active["id"])
    assert persisted["state"] == "active"
    assert persisted["activation_wake_delivered_at"] is not None
    assert supervisor.supervisor_is_healthy(active["id"])
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revision_conflict", "lease_removed"])
async def test_wake_retry_conflict_gives_up_without_touching_lease(
    isolated_db,
    monkeypatch,
    mutation,
):
    """A lease change between retry delivery and receipt recording gives up.

    When the lease revision moves (or the row vanishes) after the retry's
    successful wake delivery but before record_activation_wake_delivery, the
    revision-bound record step raises LeaseConflictError/LeaseNotFoundError.
    The retry handles it on its specific conflict branch (warning, not the
    unexpected-failure catch-all), leaves the lease's current state exactly
    as the concurrent writer left it, and fabricates no wake receipt.
    """
    from host_access import supervisor
    from infra.database import get_db
    from loguru import logger

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor, "_ACTIVATION_WAKE_RETRY_DELAY_SECONDS", 0.0
    )
    warnings: list[str] = []
    unexpected: list[str] = []
    monkeypatch.setattr(
        logger,
        "warning",
        lambda message, *args, **_kwargs: warnings.append(
            message.format(*args) if args else message
        ),
    )
    monkeypatch.setattr(
        logger,
        "exception",
        lambda message, *args, **_kwargs: unexpected.append(
            message.format(*args) if args else message
        ),
    )
    wake_calls: list[int] = []

    async def wake(lease):
        wake_calls.append(1)
        if len(wake_calls) == 1:
            return False
        db = get_db()
        if mutation == "revision_conflict":
            db.execute(
                "UPDATE host_access_leases SET revision=revision+1 "
                "WHERE id=?",
                (lease["id"],),
            )
        else:
            db.execute(
                "DELETE FROM host_access_leases WHERE id=?",
                (lease["id"],),
            )
        db.commit()
        db.close()
        return True

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    owner = supervisor._owners[active["id"]]
    assert owner.wake_retry_task is not None
    await owner.wake_retry_task

    assert len(wake_calls) == 2
    assert any(
        active["id"] in message and "stays absent" in message
        for message in warnings
    )
    assert unexpected == []
    db = get_db()
    row = db.execute(
        "SELECT state, revision, activation_wake_delivered_at "
        "FROM host_access_leases WHERE id=?",
        (active["id"],),
    ).fetchone()
    db.close()
    if mutation == "revision_conflict":
        assert row is not None
        assert row[0] == "active"
        assert row[1] == active["revision"] + 1
        assert row[2] is None
    else:
        assert row is None
    assert supervisor._owner_public(owner)[
        "activation_wake_delivered_at"
    ] is None
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_first_action_after_failed_wake_reviews_normally(
    isolated_db,
    monkeypatch,
):
    """A lease with an undelivered wake flows through normal action review."""
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor, "_ACTIVATION_WAKE_RETRY_DELAY_SECONDS", 0.0
    )
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=False),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    owner = supervisor._owners[active["id"]]
    assert owner.wake_retry_task is not None
    await owner.wake_retry_task
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review("allow")),
    )

    result = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="a" * 64,
        redacted_descriptor=_bash_descriptor(),
    )

    assert result["review"]["verdict"] == "allow"
    assert result["review"]["permit_id"]
    assert result["lease"]["state"] == "active"
    persisted = get_lease(active["id"])
    assert persisted["state"] == "active"
    assert persisted["activation_wake_delivered_at"] is None
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_owner_arming_failure_still_blocks_activation(
    isolated_db,
    monkeypatch,
):
    """Genuine activation failures keep today's terminal behavior."""
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    wake_attempted = False

    async def wake(_lease):
        nonlocal wake_attempted
        wake_attempted = True
        return True

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)

    def failing_arm(_owner, _lease):
        raise supervisor.HostAccessSupervisorError(
            "host-access activation identity is invalid"
        )

    monkeypatch.setattr(supervisor, "_arm_owner", failing_arm)
    blocked = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "activation_identity_failed"
    assert wake_attempted is False
    assert supervisor.host_access_supervisor_count() == 0
    assert await supervisor.resolve_active_host_access_lease("root") is None


@pytest.mark.asyncio
async def test_activation_rejects_catalog_identity_remap_after_review(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    remapped = SimpleNamespace(
        **{
            **vars(TARGET),
            "transport_model": "openai/remapped-reviewer",
            "expected_reported_model": "openai/remapped-reviewer",
            "reported_model": "openai/remapped-reviewer",
        }
    )

    async def resolve(expected_catalog_id=None, **_kwargs):
        assert expected_catalog_id == TARGET.catalog_id
        return remapped

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    blocked = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "activation_failed"
    assert supervisor.host_access_supervisor_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "state"),
    [("allow", "active"), ("block", "active"), ("revoke", "revoked")],
)
async def test_action_verdict_is_durable_and_has_exact_effect(
    isolated_db,
    monkeypatch,
    verdict,
    state,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "child", spawned_by="root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    async def review_action(_target, _application, action):
        assert action["session_id"] == "child"
        return _action_review(verdict)

    monkeypatch.setattr(supervisor, "_invoke_action_review", review_action)
    result = await supervisor.review_host_action(
        active["id"],
        thread_id="child",
        tool_name="bash",
        action_hash="a" * 64,
        redacted_descriptor=_bash_descriptor(),
    )

    assert result["review"]["verdict"] == verdict
    assert result["lease"]["state"] == state
    if state == "revoked":
        assert supervisor.host_access_supervisor_count() == 0
    else:
        await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_unredacted_bash_descriptor_never_reaches_reviewer(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "child", spawned_by="root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    reviewer_called = False

    async def unexpected(*_args, **_kwargs):
        nonlocal reviewer_called
        reviewer_called = True
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", unexpected)
    with pytest.raises(
        supervisor.HostAccessSupervisorError,
        match="failed closed",
    ):
        await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor={
                "version": 1,
                "tool": "bash",
                "command": (
                    "curl --user alice:raw-secret https://example.test"
                ),
            },
        )

    assert get_lease(active["id"])["state"] == "blocked"
    assert reviewer_called is False
    assert supervisor.host_access_supervisor_count() == 0


@pytest.mark.asyncio
async def test_action_reviews_are_serialized_and_revalidate_after_await(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "child", spawned_by="root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_review(_target, _application, _action):
        entered.set()
        await release.wait()
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", delayed_review)
    pending = asyncio.create_task(
        supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )
    )
    await entered.wait()
    await supervisor.revoke_active_lease(
        active["id"],
        user_id="steve",
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
    )
    release.set()

    with pytest.raises(supervisor.HostAccessSupervisorError):
        await pending
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_action_rechecks_exact_reviewer_after_inference(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "child", spawned_by="root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    changed = False
    resolves = 0

    async def resolve(expected_catalog_id=None, **_kwargs):
        nonlocal resolves
        resolves += 1
        assert expected_catalog_id == TARGET.catalog_id
        if changed:
            return SimpleNamespace(
                **{
                    **vars(TARGET),
                    "transport_model": "openai/remapped-reviewer",
                    "expected_reported_model": "openai/remapped-reviewer",
                    "reported_model": "openai/remapped-reviewer",
                }
            )
        return TARGET

    async def review_action(*_args):
        nonlocal changed
        changed = True
        return _action_review()

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(supervisor, "_invoke_action_review", review_action)

    with pytest.raises(supervisor.HostAccessSupervisorError):
        await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )

    assert resolves == 2
    assert get_lease(active["id"])["state"] == "blocked"


@pytest.mark.asyncio
async def test_reviewer_failure_denies_action_keeps_lease_and_owner_death_blocks(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "child", spawned_by="root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    async def explode(*_args):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(supervisor, "_invoke_action_review", explode)
    # A reviewer-invocation failure denies the one action with a specific
    # reason but never terminalizes the healthy armed lease.
    with pytest.raises(
        supervisor.ActionReviewUnavailableError,
        match="rate limited",
    ):
        await supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )
    assert get_lease(active["id"])["state"] == "active"
    assert await supervisor.resolve_active_host_access_lease("child") is not None

    # The next attempt re-reviews: nothing was skipped and nothing is stuck.
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review("allow")),
    )
    retried = await supervisor.review_host_action(
        active["id"],
        thread_id="child",
        tool_name="bash",
        action_hash="a" * 64,
        redacted_descriptor=_bash_descriptor(),
    )
    assert retried["review"]["verdict"] == "allow"
    assert get_lease(active["id"])["state"] == "active"

    _seed_session(isolated_db, "root-2")
    reviewed_2 = await supervisor.request_reviewed_lease(
        "root-2",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )
    active_2 = await supervisor.approve_and_activate(
        reviewed_2["id"],
        user_id="steve",
        expected_revision=reviewed_2["revision"],
        application_hash=reviewed_2["application_hash"],
    )
    owner = supervisor._owners[active_2["id"]]
    owner.task.cancel()
    await asyncio.gather(owner.task, return_exceptions=True)
    assert get_lease(active_2["id"])["state"] == "blocked"


@pytest.mark.asyncio
async def test_cancelling_owner_is_unhealthy_before_fail_closed_cleanup_finishes(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    _seed_session(isolated_db, "child", spawned_by="root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    review_entered = asyncio.Event()
    review_release = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def delayed_review(*_args):
        review_entered.set()
        await review_release.wait()
        return _action_review()

    async def delayed_cleanup(_lease):
        cleanup_entered.set()
        await cleanup_release.wait()

    monkeypatch.setattr(supervisor, "_invoke_action_review", delayed_review)
    supervisor.configure_host_access_terminal_callback(delayed_cleanup)
    pending = asyncio.create_task(
        supervisor.review_host_action(
            active["id"],
            thread_id="child",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )
    )
    await review_entered.wait()
    owner = supervisor._owners[active["id"]]
    owner.task.cancel()
    await cleanup_entered.wait()

    assert not supervisor.supervisor_is_healthy(active["id"])
    review_release.set()
    assert get_lease(active["id"])["state"] == "blocked"
    await asyncio.sleep(0)
    assert not pending.done()

    cleanup_release.set()
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await pending
    await asyncio.gather(owner.task, return_exceptions=True)
    assert get_lease(active["id"])["state"] == "blocked"
    supervisor.configure_host_access_terminal_callback(None)


@pytest.mark.asyncio
async def test_approval_retry_cannot_replace_a_dying_active_owner(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def delayed_cleanup(_lease):
        cleanup_entered.set()
        await cleanup_release.wait()

    supervisor.configure_host_access_terminal_callback(delayed_cleanup)
    owner = supervisor._owners[active["id"]]
    owner.task.cancel()
    await cleanup_entered.wait()

    retry = asyncio.create_task(
        supervisor.approve_and_activate(
            active["id"],
            user_id="steve",
            expected_revision=active["revision"],
            application_hash=active["application_hash"],
        )
    )
    await asyncio.sleep(0)
    assert not retry.done()
    assert not supervisor.supervisor_is_healthy(active["id"])
    cleanup_release.set()
    blocked = await retry
    await asyncio.gather(owner.task, return_exceptions=True)
    supervisor.configure_host_access_terminal_callback(None)
    assert blocked["state"] == "blocked"


@pytest.mark.asyncio
async def test_shutdown_blocks_live_lease_instead_of_reattaching(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    await supervisor.shutdown_host_access_supervisors()

    assert supervisor.host_access_supervisor_count() == 0
    stored = get_lease(active["id"])
    assert stored["state"] == "blocked"
    assert stored["terminal_reason"] == "backend_shutdown"


@pytest.mark.asyncio
async def test_boot_recovery_blocks_every_nonterminal_without_reattachment(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        get_lease,
        record_activation_wake_delivery,
        record_application_review,
        request_lease,
    )

    for thread_id in (
        "reviewing",
        "waiting",
        "activating",
        "active",
        "unconfirmed",
    ):
        _seed_session(isolated_db, thread_id)

    def request(thread_id):
        return request_lease(
            thread_id,
            purpose="Inspect host state",
            plan=["Read process state"],
            duration_seconds=60,
        )

    reviewing = request("reviewing")
    waiting = request("waiting")
    activating = request("activating")
    active = request("active")
    unconfirmed = request("unconfirmed")
    for name, row in (
        ("waiting", waiting),
        ("activating", activating),
        ("active", active),
        ("unconfirmed", unconfirmed),
    ):
        reviewed = record_application_review(
            row["id"],
            expected_revision=row["revision"],
            application_hash=row["application_hash"],
            allowed=True,
            reviewer_model=TARGET.catalog_id,
                review_result={
                    "attestation": "Bounded.",
                    "reviewer_display_name": TARGET.display_name,
                    "reviewer_harness": TARGET.harness,
                    "reviewer_transport_model": TARGET.transport_model,
                    "reviewer_reported_model": TARGET.reported_model,
                },
        )
        if name == "waiting":
            waiting = reviewed
            continue
        approved = approve_lease(
            reviewed["id"],
            user_id="steve",
            expected_revision=reviewed["revision"],
            application_hash=reviewed["application_hash"],
        )
        if name == "activating":
            activating = approved
        else:
            activated = activate_lease(
                approved["id"],
                expected_revision=approved["revision"],
                application_hash=approved["application_hash"],
            )
            if name == "active":
                active = record_activation_wake_delivery(
                    activated["id"],
                    expected_revision=activated["revision"],
                    application_hash=activated["application_hash"],
                )
            else:
                unconfirmed = activated

    _configure_review_fakes(monkeypatch, supervisor)
    wakes = []

    async def wake(lease):
        wakes.append(lease["id"])
        return True

    monkeypatch.setattr(supervisor, "_deliver_activation_wake", wake)

    outcome = await supervisor.recover_host_access_supervisors()

    assert get_lease(reviewing["id"])["state"] == "blocked"
    assert get_lease(waiting["id"])["state"] == "blocked"
    assert get_lease(activating["id"])["state"] == "blocked"
    assert get_lease(active["id"])["state"] == "blocked"
    assert get_lease(unconfirmed["id"])["state"] == "blocked"
    assert set(outcome) == {
        reviewing["id"],
        waiting["id"],
        activating["id"],
        active["id"],
        unconfirmed["id"],
    }
    assert wakes == []
    assert supervisor.host_access_supervisor_count() == 0


def test_application_payload_uses_root_thread_id() -> None:
    from host_access import supervisor

    payload = supervisor._application_payload({
        "id": "lease-1",
        "application_hash": "a" * 64,
        "root_thread_id": "root-usage",
        "purpose": "Inspect host state",
        "plan": ["Inspect"],
        "requested_duration_seconds": 300,
        "expected_resources": None,
        "persistent_effects": None,
        "rollback_plan": None,
        "forbidden_effects": None,
        "requesting_project_id": None,
        "repo_policy_snapshot": [],
        "repo_policy_hash": "b" * 64,
        "requested_policy_exceptions": [],
    })

    assert payload["root_thread_id"] == "root-usage"
    assert "root_session_id" not in payload


@pytest.mark.asyncio
async def test_review_invokers_supply_exact_root_thread_for_usage(
    monkeypatch,
) -> None:
    from host_access import reviewer, supervisor

    application = {
        "root_thread_id": "root-usage",
        "purpose": "Inspect host state",
    }
    calls: list[tuple[str, str | None]] = []

    async def review_application(_target, _application, **kwargs):
        calls.append(("application", kwargs.get("usage_thread_id")))
        return _application_review()

    async def review_action(_target, _application, _action, **kwargs):
        calls.append(("action", kwargs.get("usage_thread_id")))
        return _action_review()

    monkeypatch.setattr(reviewer, "review_application", review_application)
    monkeypatch.setattr(reviewer, "review_action", review_action)

    await supervisor._invoke_application_review(
        TARGET,
        application,
        auto_approve=False,
    )
    await supervisor._invoke_action_review(TARGET, application, {"action_hash": "a"})

    assert calls == [
        ("application", "root-usage"),
        ("action", "root-usage"),
    ]


@pytest.mark.asyncio
async def test_first_reviewed_action_after_activation_reviews_and_keeps_lease(
    isolated_db,
    monkeypatch,
):
    """Regression for lease cc54c91a: the first action must never kill a lease.

    Immediately after approve_and_activate, the first reviewed action for the
    same session that holds the lease resolves, reviews, grants a permit, and
    leaves the lease active and resolvable.
    """
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review("allow")),
    )

    resolved = await supervisor.resolve_active_host_access_lease("root")
    assert resolved is not None and resolved["id"] == active["id"]
    result = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="a" * 64,
        redacted_descriptor=_bash_descriptor(),
    )

    assert result["review"]["verdict"] == "allow"
    assert result["review"]["permit_id"]
    assert result["lease"]["state"] == "active"
    assert get_lease(active["id"])["state"] == "active"
    assert await supervisor.resolve_active_host_access_lease("root") is not None


@pytest.mark.asyncio
async def test_reviewer_target_resolution_failure_denies_action_keeps_lease(
    isolated_db,
    monkeypatch,
):
    """An unavailable reviewer model denies the action, not the lease."""
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session(isolated_db, "root")
    reviewed = await _reviewed_request(monkeypatch, supervisor)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    async def unavailable(_expected_catalog_id=None):
        raise supervisor.HostAccessSupervisorError(
            "configured host-access reviewer is unavailable"
        )

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", unavailable)
    with pytest.raises(
        supervisor.ActionReviewUnavailableError,
        match="reviewer is unavailable",
    ):
        await supervisor.review_host_action(
            active["id"],
            thread_id="root",
            tool_name="bash",
            action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )
    assert get_lease(active["id"])["state"] == "active"
    assert await supervisor.resolve_active_host_access_lease("root") is not None


@pytest.mark.asyncio
async def test_review_failure_distinguishes_causes_for_the_holder(
    isolated_db,
    monkeypatch,
):
    """Rate limit, invalid output, and transport loss need different corrections.

    Collapsing them all to "application_reviewer_failure" leaves the holder
    unable to choose between waiting, retrying, fixing configuration, and
    replacing a reviewer model that cannot emit schema-valid output.
    """
    from host_access import supervisor
    from host_access.reviewer import HostAccessReviewValidationError
    from llm.isolated_structured import (
        IsolatedStructuredOutputTruncatedError,
        IsolatedStructuredProviderError,
        IsolatedStructuredRateLimitError,
    )

    _configure_review_fakes(monkeypatch, supervisor, application="allow")

    cases = (
        ("ratelimited", IsolatedStructuredRateLimitError("429"), "rate_limit"),
        ("provider", IsolatedStructuredProviderError("provider_error:http_503"), "provider_error"),
        ("truncated", IsolatedStructuredOutputTruncatedError("reviewer_output_truncated"), "reviewer_output_truncated"),
        ("badoutput", HostAccessReviewValidationError("bad"), "invalid_output"),
    )
    seen: list[str] = []
    for session, error, expected_cause in cases:
        _seed_session(isolated_db, session)

        async def explode(_target, _payload, *, auto_approve=False, _err=error):
            raise _err

        monkeypatch.setattr(supervisor, "_invoke_application_review", explode)
        lease = await supervisor.request_reviewed_lease(
            session,
            purpose="Inspect host state",
            plan=["Read process state"],
            duration_seconds=60,
        )

        assert lease["state"] == "blocked"
        reason = lease["terminal_reason"]
        assert reason.startswith("application_reviewer_failure")
        # A bounded cause the holder can act on: wait out a limit versus
        # replace a reviewer that cannot emit schema-valid output.
        assert expected_cause in reason
        # The class name is deliberately absent: it is implementation detail,
        # and the bounded cause is the stable signal the holder acts on.
        assert type(error).__name__ not in reason
        seen.append(reason)

    # The whole point: two different failures are not the same string.
    assert seen[0] != seen[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised",
    [ConnectionError("connection refused"), TimeoutError("call hung"), KeyError("k")],
    ids=["connection", "timeout", "local-defect"],
)
async def test_unrecognised_failures_name_the_stage_without_asserting_a_cause(
    isolated_db,
    monkeypatch,
    raised,
):
    """Only failures the code recognises carry a cause.

    An unwrapped network error, a bare timeout, and a local defect all reach
    the same handler and none of them is a cause this code can establish. It
    reports the stage alone rather than telling the holder to retry (or to
    stop retrying) on a guess.
    """
    from host_access import supervisor

    _configure_review_fakes(monkeypatch, supervisor, application="allow")
    _seed_session(isolated_db, "unrecognised")

    async def explode(_target, _payload, *, auto_approve=False):
        raise raised

    monkeypatch.setattr(supervisor, "_invoke_application_review", explode)
    lease = await supervisor.request_reviewed_lease(
        "unrecognised",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert lease["state"] == "blocked"
    assert lease["terminal_reason"] == "application_reviewer_failure"


@pytest.mark.asyncio
async def test_review_failure_reason_stays_single_line_and_bounded(
    isolated_db,
    monkeypatch,
):
    """A raw provider dump must not become the durable terminal reason."""
    from host_access import supervisor
    from infra.host_access_leases import MAX_TERMINAL_REASON_CHARS

    _configure_review_fakes(monkeypatch, supervisor, application="allow")
    _seed_session(isolated_db, "noisy")

    async def explode(_target, _payload, *, auto_approve=False):
        raise RuntimeError("line one\nline two\r\n" + "x" * 5000)

    monkeypatch.setattr(supervisor, "_invoke_application_review", explode)
    lease = await supervisor.request_reviewed_lease(
        "noisy",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    reason = lease["terminal_reason"]
    assert lease["state"] == "blocked"
    assert "\n" not in reason and "\r" not in reason
    assert len(reason) < MAX_TERMINAL_REASON_CHARS
    assert reason.startswith("application_reviewer_failure")


@pytest.mark.asyncio
async def test_review_failure_reason_carries_no_provider_message_text(
    isolated_db,
    monkeypatch,
):
    """Nothing from the third-party message reaches the durable record.

    Pattern-based redaction is best-effort, so the reason is built from a
    bounded cause only. A secret embedded anywhere in a provider error cannot
    be persisted, whatever shape it takes, and the exception class name is
    likewise absent.
    """
    from host_access import supervisor

    _configure_review_fakes(monkeypatch, supervisor, application="allow")
    _seed_session(isolated_db, "leaky")
    planted = "unrecognized-secret-shape-0123456789"

    async def explode(_target, _payload, *, auto_approve=False):
        raise RuntimeError("auth rejected for " + planted)

    monkeypatch.setattr(supervisor, "_invoke_application_review", explode)
    lease = await supervisor.request_reviewed_lease(
        "leaky",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    reason = lease["terminal_reason"]
    assert lease["state"] == "blocked"
    assert planted not in reason
    assert "auth rejected" not in reason
    # The class name is absent too: only the bounded cause survives.
    assert "RuntimeError" not in reason
    assert reason.startswith("application_reviewer_failure")


@pytest.mark.asyncio
async def test_unclassified_review_failure_logs_its_sanitized_cause_chain(
    isolated_db,
    monkeypatch,
):
    """The operator can diagnose a stage-only failure from the server log.

    The holder still sees the bare stage, but the log names every link of the
    chain and where it was raised, with recognized credentials redacted.
    """
    from host_access import supervisor
    from loguru import logger

    _configure_review_fakes(monkeypatch, supervisor, application="allow")
    _seed_session(isolated_db, "diagnosable")
    warnings: list[str] = []
    monkeypatch.setattr(
        logger,
        "warning",
        lambda message, *args, **_kwargs: warnings.append(message.format(*args)),
    )

    async def explode(_target, _payload, *, auto_approve=False):
        try:
            raise ValueError("upstream said token=sk-live-0123456789")
        except ValueError as exc:
            raise supervisor.HostAccessSupervisorError(
                "model browser isolation could not be established"
            ) from exc

    monkeypatch.setattr(supervisor, "_invoke_application_review", explode)
    lease = await supervisor.request_reviewed_lease(
        "diagnosable",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    assert lease["terminal_reason"] == "application_reviewer_failure"
    [logged] = [w for w in warnings if "application flow failed" in w]
    assert (
        "HostAccessSupervisorError: model browser isolation could not be "
        "established at "
    ) in logged
    assert " <- ValueError: upstream said token=[REDACTED] at " in logged
    assert "in explode" in logged
    assert "sk-live-0123456789" not in logged


@pytest.mark.asyncio
async def test_activation_failure_after_a_recorded_review_names_its_stage(
    isolated_db,
    monkeypatch,
):
    """The other half of the behaviour table: the review succeeded already.

    A failure after the verdict is durable must not be reported as a reviewer
    failure, because the correction is entirely different. The failure is
    injected at the real activation step (`approve_and_activate`) that runs
    after the review is recorded, not at an incidental notify point.
    """
    from host_access import supervisor
    from infra.settings import save_agent_defaults

    _configure_review_fakes(monkeypatch, supervisor, application="allow")
    _seed_session(isolated_db, "postreview")
    save_agent_defaults({"host_access_auto_approve": True})

    async def boom_activation(*_args, **_kwargs):
        raise RuntimeError("activation exploded after the verdict was recorded")

    monkeypatch.setattr(supervisor, "approve_and_activate", boom_activation)
    lease = await supervisor.request_reviewed_lease(
        "postreview",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )

    # Exact equality: not the reviewer stage, and no provider text.
    assert lease["state"] == "blocked"
    assert lease["terminal_reason"] == "activation_failed"
