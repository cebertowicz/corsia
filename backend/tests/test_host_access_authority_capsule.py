from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sqlite3
import threading
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


@pytest.fixture(autouse=True)
async def _clear_process_private_owners():
    """Keep an intentionally failing test from leaking owner tasks onward."""
    from host_access import supervisor

    yield
    for owner in tuple(supervisor._owners.values()):
        owner.graceful = True
        owner.stopping = True
        if owner.task is not None and not owner.task.done():
            owner.task.cancel()
        if owner.task is not None:
            await asyncio.gather(owner.task, return_exceptions=True)
    supervisor._owners.clear()
    supervisor.configure_host_access_terminal_callback(None)


def _application_review():
    return SimpleNamespace(
        identity=IDENTITY,
        schema_version=3,
        verdict="allow",
        reason_category="not_applicable",
        reason="The request is bounded.",
        user_attestation="This request matches the stated plan.",
        reviewer_fallback=False,
        allowed=True,
    )


def _action_review(verdict: str = "allow"):
    return SimpleNamespace(
        identity=IDENTITY,
        schema_version=1,
        verdict=verdict,
        reason="The action matches the approved plan.",
        allowed=verdict == "allow",
    )


def _descriptor() -> dict:
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
    thread_id: str,
    *,
    user_id: str = "steve",
    spawned_by: str | None = None,
) -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,spawned_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (thread_id, user_id, thread_id, spawned_by, "t", "t"),
    )
    db.commit()
    db.close()


def _configure_reviews(monkeypatch, supervisor) -> None:
    async def resolve(expected_catalog_id=None, **_kwargs):
        if expected_catalog_id is not None:
            assert expected_catalog_id == TARGET.catalog_id
        return TARGET

    async def application(_target, _payload, *, auto_approve=False):
        return _application_review()

    async def close_browsers(_user_id):
        return None

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(supervisor, "_invoke_application_review", application)
    monkeypatch.setattr(supervisor, "_close_user_model_browsers", close_browsers)
    monkeypatch.setattr(
        supervisor,
        "_deliver_activation_wake",
        lambda _lease: asyncio.sleep(0, result=True),
    )


async def _activate(monkeypatch, supervisor, thread_id: str) -> dict:
    _configure_reviews(monkeypatch, supervisor)
    reviewed = await supervisor.request_reviewed_lease(
        thread_id,
        purpose=f"Inspect host state from {thread_id}",
        plan=["Read process state"],
        duration_seconds=60,
    )
    return await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )


@pytest.mark.asyncio
async def test_owner_lineage_is_frozen_and_nearest_nested_lease_wins(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    _seed_session("child", spawned_by="root")
    _seed_session("grandchild", spawned_by="child")
    outer = await _activate(monkeypatch, supervisor, "root")
    inner = await _activate(monkeypatch, supervisor, "child")

    resolved = await supervisor.resolve_active_host_access_lease("grandchild")
    assert resolved is not None and resolved["id"] == inner["id"]

    db = get_db()
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,spawned_by,created_at,updated_at) "
        "VALUES ('forged','steve','forged','root','t','t')"
    )
    db.execute(
        "UPDATE threads SET spawned_by='root' WHERE id='grandchild'"
    )
    db.commit()
    db.close()

    assert await supervisor.resolve_active_host_access_lease("forged") is None
    assert (
        await supervisor.resolve_active_host_access_lease("grandchild")
    )["id"] == inner["id"]

    await supervisor.revoke_active_lease(
        inner["id"],
        user_id="steve",
        expected_revision=inner["revision"],
        application_hash=inner["application_hash"],
    )
    fallback = await supervisor.resolve_active_host_access_lease("grandchild")
    assert fallback is not None and fallback["id"] == outer["id"]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_visible_creation_registers_descendant_before_broadcast(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra import fresh_threads

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    observed: list[str] = []

    def broadcast(session):
        owner = supervisor._owners[active["id"]]
        assert owner.lineages[session["id"]] == ("root", session["id"])
        observed.append(session["id"])

    monkeypatch.setattr(fresh_threads, "emit_session_created", broadcast)
    child = await fresh_threads.create_visible_fresh_thread(
        user_id="steve",
        title="Child",
        model="native/reviewer",
        spawned_by="root",
    )

    resolved = await supervisor.resolve_active_host_access_lease(child["id"])
    assert resolved is not None and resolved["id"] == active["id"]
    assert observed == [child["id"]]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_registration_never_grants_unknown_or_foreign_parentage(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")

    assert supervisor.register_host_access_descendant(
        parent_thread_id="unknown",
        thread_id="unknown-child",
        user_id="steve",
    ) is False
    assert supervisor.register_host_access_descendant(
        parent_thread_id="root",
        thread_id="foreign-child",
        user_id="other-user",
    ) is False
    assert await supervisor.resolve_active_host_access_lease(
        "unknown-child"
    ) is None
    assert await supervisor.resolve_active_host_access_lease(
        "foreign-child"
    ) is None
    assert (
        await supervisor.resolve_active_host_access_lease("root")
    )["id"] == active["id"]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_action_review_uses_frozen_application_and_private_permit(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    captured: dict = {}

    db = get_db()
    db.execute("DROP TRIGGER trg_host_access_lease_application_immutable")
    db.execute(
        "UPDATE host_access_leases SET purpose='tampered purpose',"
        "expires_at='2099-01-01T00:00:00' WHERE id=?",
        (active["id"],),
    )
    db.commit()
    db.close()

    async def action(_target, application, _action):
        captured.update(application)
        return _action_review()

    monkeypatch.setattr(supervisor, "_invoke_action_review", action)
    result = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="a" * 64,
        redacted_descriptor=_descriptor(),
    )
    assert captured["purpose"] == "Inspect host state from root"
    permit_id = result["review"]["permit_id"]

    db = get_db()
    db.execute("DELETE FROM host_access_leases WHERE id=?", (active["id"],))
    db.commit()
    db.close()

    await supervisor.consume_host_action_permit(
        permit_id,
        lease_id=active["id"],
        thread_id="root",
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
        tool_name="bash",
        action_hash="a" * 64,
    )
    with pytest.raises(
        supervisor.HostAccessSupervisorError,
        match="unavailable|consumed",
    ):
        await supervisor.consume_host_action_permit(
            permit_id,
            lease_id=active["id"],
            thread_id="root",
            expected_revision=active["revision"],
            application_hash=active["application_hash"],
            tool_name="bash",
            action_hash="a" * 64,
        )

    follow_up = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="c" * 64,
        redacted_descriptor=_descriptor(),
    )
    assert follow_up["review"]["permit_id"]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_durable_but_unissued_permit_never_authorizes(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import record_action_review

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    forged = record_action_review(
        active["id"],
        thread_id="root",
        lease_revision=active["revision"],
        tool_name="bash",
        action_hash="b" * 64,
        redacted_descriptor=_descriptor(),
        reviewer_model=TARGET.catalog_id,
        verdict="allow",
        reason="Durable row only",
        permit_expires_at=active["expires_at"],
    )

    with pytest.raises(
        supervisor.HostAccessSupervisorError,
        match="unavailable",
    ):
        await supervisor.consume_host_action_permit(
            forged["permit_id"],
            lease_id=active["id"],
            thread_id="root",
            expected_revision=active["revision"],
            application_hash=active["application_hash"],
            tool_name="bash",
            action_hash="b" * 64,
        )
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_private_permit_pop_is_burned_when_consumption_audit_fails(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review()),
    )
    result = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="d" * 64,
        redacted_descriptor=_descriptor(),
    )
    permit_id = result["review"]["permit_id"]

    def fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("audit database unavailable")

    monkeypatch.setattr(
        supervisor,
        "mark_action_permit_consumed",
        fail_audit,
    )
    await supervisor.consume_host_action_permit(
        permit_id,
        lease_id=active["id"],
        thread_id="root",
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
        tool_name="bash",
        action_hash="d" * 64,
    )
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await supervisor.consume_host_action_permit(
            permit_id,
            lease_id=active["id"],
            thread_id="root",
            expected_revision=active["revision"],
            application_hash=active["application_hash"],
            tool_name="bash",
            action_hash="d" * 64,
        )
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_reviewer_revoke_fences_before_audit_and_terminalizes_once(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review()),
    )
    allowed = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="d" * 64,
        redacted_descriptor=_descriptor(),
    )
    existing_permit = allowed["review"]["permit_id"]
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review("revoke")),
    )
    original_record = supervisor.record_action_review
    audit_entered = threading.Event()
    audit_release = threading.Event()

    def record(*args, **kwargs):
        if kwargs.get("verdict") == "revoke":
            audit_entered.set()
            if not audit_release.wait(timeout=5):
                raise AssertionError("revoke audit was not released")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(supervisor, "record_action_review", record)
    pending = asyncio.create_task(
        supervisor.review_host_action(
            active["id"],
            thread_id="root",
            tool_name="bash",
            action_hash="e" * 64,
            redacted_descriptor=_descriptor(),
        )
    )
    try:
        assert await asyncio.to_thread(audit_entered.wait, 2)
        owner = supervisor._owners[active["id"]]
        assert owner.stopping
        assert not supervisor._owner_is_active(owner)
        assert owner.permits == {}
        with pytest.raises(supervisor.HostAccessSupervisorError):
            await supervisor.consume_host_action_permit(
                existing_permit,
                lease_id=active["id"],
                thread_id="root",
                expected_revision=active["revision"],
                application_hash=active["application_hash"],
                tool_name="bash",
                action_hash="d" * 64,
            )
        assert not pending.done()
    finally:
        audit_release.set()
    result = await pending

    assert result["lease"]["state"] == "revoked"
    assert await supervisor.resolve_active_host_access_lease("root") is None


@pytest.mark.asyncio
async def test_reviewer_revoke_audit_loss_still_fences_and_cleans_once(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review("revoke")),
    )
    cleaned: list[dict] = []

    async def cleanup(lease):
        assert await supervisor.resolve_active_host_access_lease("root") is None
        cleaned.append(lease)

    def lose_projection(*_args, **_kwargs):
        db = get_db()
        db.execute(
            "DELETE FROM host_access_leases WHERE id=?",
            (active["id"],),
        )
        db.commit()
        db.close()
        raise sqlite3.OperationalError("audit row lost")

    supervisor.configure_host_access_terminal_callback(cleanup)
    monkeypatch.setattr(supervisor, "record_action_review", lose_projection)
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await supervisor.review_host_action(
            active["id"],
            thread_id="root",
            tool_name="bash",
            action_hash="f" * 64,
            redacted_descriptor=_descriptor(),
        )

    assert active["id"] not in supervisor._owners
    assert len(cleaned) == 1
    assert cleaned[0]["state"] == "blocked"
    assert cleaned[0]["terminal_reason"] == "action_reviewer_failure"


@pytest.mark.asyncio
async def test_user_revoke_fences_owner_before_durable_projection_and_cleanup(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    original_projection = supervisor.terminate_lease_projection
    cleaned: list[str] = []

    def project(*args, **kwargs):
        owner = supervisor._owners[active["id"]]
        assert owner.stopping
        assert not supervisor._owner_is_active(owner)
        return original_projection(*args, **kwargs)

    async def cleanup(lease):
        assert await supervisor.resolve_active_host_access_lease("root") is None
        cleaned.append(lease["id"])

    monkeypatch.setattr(supervisor, "terminate_lease_projection", project)
    supervisor.configure_host_access_terminal_callback(cleanup)
    revoked = await supervisor.revoke_active_lease(
        active["id"],
        user_id="steve",
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
    )

    assert revoked["state"] == "revoked"
    assert cleaned == [active["id"]]


@pytest.mark.asyncio
async def test_deleted_active_row_remains_visible_and_user_revocable(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    db = get_db()
    db.execute("DELETE FROM host_access_leases WHERE id=?", (active["id"],))
    db.commit()
    db.close()

    listed = await supervisor.list_host_access_leases("steve")
    assert [row["id"] for row in listed] == [active["id"]]
    scoped = await supervisor.list_host_access_leases(
        "steve",
        root_thread_id="root",
        nonterminal_only=True,
    )
    assert [row["id"] for row in scoped] == [active["id"]]
    assert (
        await supervisor.get_host_access_lease(active["id"], user_id="steve")
    )["state"] == "active"

    revoked = await supervisor.revoke_active_lease(
        active["id"],
        user_id="steve",
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
    )
    assert revoked["state"] == "revoked"
    assert await supervisor.resolve_active_host_access_lease("root") is None


@pytest.mark.asyncio
async def test_db_only_active_row_is_not_exposed_by_another_root_owner(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import LeaseNotFoundError

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    forged = {**active, "id": "forged-active-row"}
    monkeypatch.setattr(supervisor, "list_leases", lambda _user_id: [forged])
    monkeypatch.setattr(
        supervisor,
        "get_lease",
        lambda _lease_id, *, user_id: forged,
    )

    listed = await supervisor.list_host_access_leases("steve")
    assert [row["id"] for row in listed] == [active["id"]]
    with pytest.raises(LeaseNotFoundError, match="not found"):
        await supervisor.get_host_access_lease(
            forged["id"],
            user_id="steve",
        )
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_rearming_same_owner_cannot_extend_its_monotonic_deadline(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    owner = supervisor._owners[active["id"]]
    original_deadline = owner.monotonic_deadline

    supervisor._arm_owner(owner, active)

    assert owner.monotonic_deadline == original_deadline
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_live_owner_approval_retry_ignores_deleted_projection(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    _configure_reviews(monkeypatch, supervisor)
    reviewed = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state from root",
        plan=["Read process state"],
        duration_seconds=60,
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    db = get_db()
    db.execute("DELETE FROM host_access_leases WHERE id=?", (active["id"],))
    db.commit()
    db.close()

    retried = await supervisor.approve_and_activate(
        active["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=active["application_hash"],
    )

    assert retried == active
    assert (
        await supervisor.resolve_active_host_access_lease("root")
    )["id"] == active["id"]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_live_owner_approval_retry_ignores_projection_mutation(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    _configure_reviews(monkeypatch, supervisor)
    reviewed = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state from root",
        plan=["Read process state"],
        duration_seconds=60,
    )
    active = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    db = get_db()
    db.execute(
        "UPDATE host_access_leases SET activation_wake_delivered_at=NULL "
        "WHERE id=?",
        (active["id"],),
    )
    db.commit()
    db.close()

    retried = await supervisor.approve_and_activate(
        active["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=active["application_hash"],
    )

    assert retried == active
    assert active["id"] in supervisor._owners
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_post_arm_projection_failure_terminalizes_and_cleans(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session("root")
    _configure_reviews(monkeypatch, supervisor)
    reviewed = await supervisor.request_reviewed_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )
    cleaned: list[str] = []

    def inconsistent_projection(lease_id, **_kwargs):
        return {**get_lease(lease_id), "state": "blocked"}

    async def cleanup(lease):
        cleaned.append(lease["id"])

    monkeypatch.setattr(
        supervisor,
        "record_activation_wake_delivery",
        inconsistent_projection,
    )
    supervisor.configure_host_access_terminal_callback(cleanup)
    blocked = await supervisor.approve_and_activate(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )

    assert blocked["state"] == "blocked"
    assert blocked["terminal_reason"] == "activation_owner_unavailable"
    assert get_lease(reviewed["id"])["state"] == "blocked"
    assert cleaned == [reviewed["id"]]


@pytest.mark.asyncio
async def test_list_db_failure_requires_meaningful_live_owner_fallback(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")

    def fail_list(_user_id, **_filters):
        raise sqlite3.OperationalError("lease projection unavailable")

    monkeypatch.setattr(supervisor, "list_leases", fail_list)
    listed = await supervisor.list_host_access_leases("steve")
    assert [row["id"] for row in listed] == [active["id"]]

    await supervisor.shutdown_host_access_supervisors()
    with pytest.raises(sqlite3.OperationalError, match="projection unavailable"):
        await supervisor.list_host_access_leases("steve")


@pytest.mark.asyncio
async def test_user_revoke_racing_action_review_terminalizes_once(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned: list[dict] = []

    async def delayed_review(*_args):
        entered.set()
        await release.wait()
        return _action_review()

    async def cleanup(lease):
        cleaned.append(lease)

    monkeypatch.setattr(supervisor, "_invoke_action_review", delayed_review)
    supervisor.configure_host_access_terminal_callback(cleanup)
    pending = asyncio.create_task(
        supervisor.review_host_action(
            active["id"],
            thread_id="root",
            tool_name="bash",
            action_hash="1" * 64,
            redacted_descriptor=_descriptor(),
        )
    )
    await entered.wait()
    revoked = await supervisor.revoke_active_lease(
        active["id"],
        user_id="steve",
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
    )
    release.set()
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await pending

    assert revoked["state"] == "revoked"
    assert [(row["id"], row["state"]) for row in cleaned] == [
        (active["id"], "revoked")
    ]


@pytest.mark.asyncio
async def test_terminal_notification_failure_still_cleans_exactly_once(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    owner = supervisor._owners[active["id"]]
    cleaned: list[str] = []

    def fail_notification(_lease):
        raise RuntimeError("notification failed")

    async def cleanup(lease):
        cleaned.append(lease["id"])

    monkeypatch.setattr(supervisor, "_notify_changed", fail_notification)
    supervisor.configure_host_access_terminal_callback(cleanup)
    with pytest.raises(RuntimeError, match="notification failed"):
        await supervisor._terminalize_owner(
            owner,
            state="revoked",
            reason="user_revoked",
        )
    with pytest.raises(RuntimeError, match="notification failed"):
        await supervisor._terminalize_owner(
            owner,
            state="blocked",
            reason="second_terminal_intent",
        )

    assert cleaned == [active["id"]]


@pytest.mark.asyncio
async def test_ownerless_active_block_winner_cleans_once_and_terminal_retry_does_not(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    owner = supervisor._owners.pop(active["id"])
    owner.graceful = True
    owner.stopping = True
    assert owner.task is not None
    owner.task.cancel()
    await asyncio.gather(owner.task, return_exceptions=True)
    cleaned: list[str] = []

    async def cleanup(lease):
        cleaned.append(lease["id"])

    supervisor.configure_host_access_terminal_callback(cleanup)
    blocked = await supervisor._safe_block(
        active["id"],
        "host_access_supervisor_unavailable",
    )
    retried = await supervisor._safe_block(
        active["id"],
        "duplicate_terminal_attempt",
    )

    assert blocked["state"] == "blocked"
    assert retried["state"] == "blocked"
    assert cleaned == [active["id"]]


@pytest.mark.asyncio
async def test_ownerless_block_conflict_does_not_duplicate_winner_cleanup(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import LeaseConflictError, request_lease

    _seed_session("root")
    lease = request_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )
    original_block = supervisor.block_lease
    effects: list[str] = []

    def concurrent_winner(*args, **kwargs):
        original_block(*args, **kwargs)
        effects.append("winner-cleanup")
        raise LeaseConflictError("lease changed concurrently")

    async def duplicate_cleanup(_lease):
        effects.append("duplicate-cleanup")

    monkeypatch.setattr(supervisor, "block_lease", concurrent_winner)
    supervisor.configure_host_access_terminal_callback(duplicate_cleanup)
    blocked = await supervisor._safe_block(
        lease["id"],
        "host_access_supervisor_unavailable",
    )

    assert blocked["state"] == "blocked"
    assert effects == ["winner-cleanup"]


@pytest.mark.asyncio
async def test_ownerless_terminal_notification_failure_still_cleans_once(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import request_lease

    _seed_session("root")
    lease = request_lease(
        "root",
        purpose="Inspect host state",
        plan=["Read process state"],
        duration_seconds=60,
    )
    cleaned: list[str] = []

    def fail_notification(_lease):
        raise RuntimeError("notification failed")

    async def cleanup(terminal):
        cleaned.append(terminal["id"])

    monkeypatch.setattr(supervisor, "_notify_changed", fail_notification)
    supervisor.configure_host_access_terminal_callback(cleanup)
    with pytest.raises(RuntimeError, match="notification failed"):
        await supervisor._safe_block(
            lease["id"],
            "host_access_supervisor_unavailable",
        )

    assert cleaned == [lease["id"]]


@pytest.mark.asyncio
async def test_policy_drift_at_permit_consumption_burns_and_fences(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review()),
    )
    issued = await supervisor.review_host_action(
        active["id"],
        thread_id="root",
        tool_name="bash",
        action_hash="2" * 64,
        redacted_descriptor=_descriptor(),
    )
    permit_id = issued["review"]["permit_id"]
    monkeypatch.setattr(
        supervisor,
        "_repo_policy_is_current",
        lambda _application: asyncio.sleep(0, result=False),
    )

    with pytest.raises(
        supervisor.HostAccessSupervisorError,
        match="project policy changed",
    ):
        await supervisor.consume_host_action_permit(
            permit_id,
            lease_id=active["id"],
            thread_id="root",
            expected_revision=active["revision"],
            application_hash=active["application_hash"],
            tool_name="bash",
            action_hash="2" * 64,
        )
    with pytest.raises(supervisor.HostAccessSupervisorError):
        await supervisor.consume_host_action_permit(
            permit_id,
            lease_id=active["id"],
            thread_id="root",
            expected_revision=active["revision"],
            application_hash=active["application_hash"],
            tool_name="bash",
            action_hash="2" * 64,
        )

    stored = get_lease(active["id"])
    assert stored["state"] == "blocked"
    assert stored["terminal_reason"] == "project_policy_changed"


@pytest.mark.asyncio
async def test_frozen_owner_lineage_survives_raw_session_reparent_for_audit(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db

    _seed_session("root")
    _seed_session("child", spawned_by="root")
    active = await _activate(monkeypatch, supervisor, "root")
    db = get_db()
    db.execute("UPDATE threads SET spawned_by=NULL WHERE id='child'")
    db.commit()
    db.close()
    monkeypatch.setattr(
        supervisor,
        "_invoke_action_review",
        lambda *_args: asyncio.sleep(0, result=_action_review()),
    )

    issued = await supervisor.review_host_action(
        active["id"],
        thread_id="child",
        tool_name="bash",
        action_hash="3" * 64,
        redacted_descriptor=_descriptor(),
    )

    assert issued["review"]["verdict"] == "allow"
    assert issued["review"]["permit_id"]
    await supervisor.shutdown_host_access_supervisors()


@pytest.mark.asyncio
async def test_startup_cleans_then_blocks_every_persisted_nonterminal_row(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.database import get_db
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        record_application_review,
        request_lease,
    )

    for thread_id in ("reviewing", "waiting", "activating", "active"):
        _seed_session(thread_id)
    rows = {
        thread_id: request_lease(
            thread_id,
            purpose="Inspect host state",
            plan=["Read process state"],
            duration_seconds=60,
            now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        for thread_id in ("reviewing", "waiting", "activating", "active")
    }
    for thread_id in ("waiting", "activating", "active"):
        row = rows[thread_id]
        rows[thread_id] = record_application_review(
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
            now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    for thread_id in ("activating", "active"):
        row = rows[thread_id]
        rows[thread_id] = approve_lease(
            row["id"],
            user_id="steve",
            expected_revision=row["revision"],
            application_hash=row["application_hash"],
            now=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    row = rows["active"]
    rows["active"] = activate_lease(
        row["id"],
        expected_revision=row["revision"],
        application_hash=row["application_hash"],
        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    cleaned: list[tuple[str, str]] = []

    async def cleanup(lease):
        db = get_db()
        state = db.execute(
            "SELECT state FROM host_access_leases WHERE id=?",
            (lease["id"],),
        ).fetchone()["state"]
        db.close()
        cleaned.append((lease["id"], state))

    supervisor.configure_host_access_terminal_callback(cleanup)
    try:
        outcome = await supervisor.recover_host_access_supervisors()
    finally:
        supervisor.configure_host_access_terminal_callback(None)

    db = get_db()
    stored = dict(
        db.execute(
            "SELECT id,state FROM host_access_leases"
        ).fetchall()
    )
    db.close()
    assert set(outcome) == {row["id"] for row in rows.values()}
    assert stored == {row["id"]: "blocked" for row in rows.values()}
    assert set(cleaned) == {
        (rows[thread_id]["id"], rows[thread_id]["state"])
        for thread_id in ("reviewing", "waiting", "activating", "active")
    }


@pytest.mark.asyncio
async def test_shutdown_removes_authority_before_cleanup_and_blocks_row(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor
    from infra.host_access_leases import get_lease

    _seed_session("root")
    active = await _activate(monkeypatch, supervisor, "root")
    observed: list[object] = []

    async def cleanup(_lease):
        observed.append(
            await supervisor.resolve_active_host_access_lease("root")
        )

    supervisor.configure_host_access_terminal_callback(cleanup)
    try:
        await supervisor.shutdown_host_access_supervisors()
    finally:
        supervisor.configure_host_access_terminal_callback(None)

    assert observed == [None]
    assert get_lease(active["id"])["state"] == "blocked"
    assert get_lease(active["id"])["terminal_reason"] == "backend_shutdown"


@pytest.mark.asyncio
async def test_shutdown_fences_all_owners_before_any_cleanup_failure_escapes(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root-1")
    _seed_session("root-2")
    first = await _activate(monkeypatch, supervisor, "root-1")
    second = await _activate(monkeypatch, supervisor, "root-2")
    attempted: list[str] = []

    async def cleanup(lease):
        attempted.append(lease["id"])
        if lease["id"] == first["id"]:
            raise RuntimeError("first cleanup failed")

    supervisor.configure_host_access_terminal_callback(cleanup)
    try:
        with pytest.raises(RuntimeError, match="first cleanup failed"):
            await supervisor.shutdown_host_access_supervisors()
    finally:
        supervisor.configure_host_access_terminal_callback(None)

    assert set(attempted) == {first["id"], second["id"]}
    assert await supervisor.resolve_active_host_access_lease("root-1") is None
    assert await supervisor.resolve_active_host_access_lease("root-2") is None
    assert supervisor._owners == {}


@pytest.mark.asyncio
async def test_list_host_access_leases_scopes_live_owners_to_one_root(
    isolated_db,
    monkeypatch,
):
    from host_access import supervisor

    _seed_session("root")
    _seed_session("other-root")
    root_active = await _activate(monkeypatch, supervisor, "root")
    other_active = await _activate(monkeypatch, supervisor, "other-root")

    scoped = await supervisor.list_host_access_leases(
        "steve",
        root_thread_id="root",
        nonterminal_only=True,
    )
    unfiltered = await supervisor.list_host_access_leases("steve")

    assert [row["id"] for row in scoped] == [root_active["id"]]
    assert {row["id"] for row in unfiltered} == {
        root_active["id"],
        other_active["id"],
    }
