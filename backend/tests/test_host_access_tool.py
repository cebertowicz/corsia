from __future__ import annotations

import json
import sqlite3

import pytest

from core.turn_core import TurnIdentity


@pytest.mark.asyncio
async def test_request_host_access_tool_uses_exact_injected_session(
    isolated_db,
    monkeypatch,
):
    from infra.host_access_leases import (
        record_application_review,
        request_lease,
    )
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    db = sqlite3.connect(str(isolated_db))
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
        "VALUES ('root','steve','Root','t','t')"
    )
    db.commit()
    db.close()

    async def reviewed_request(thread_id, **application):
        requested = request_lease(thread_id, **application)
        return record_application_review(
            requested["id"],
            expected_revision=requested["revision"],
            application_hash=requested["application_hash"],
            allowed=True,
            reviewer_model="native/reviewer",
            review_result={"attestation": "Bounded."},
        )

    monkeypatch.setattr(
        host_access_tool,
        "request_reviewed_lease",
        reviewed_request,
    )
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Inspect processes", "Read logs"],
            duration_seconds=60,
            _thread_id="root",
            _host_access_turn_identity=TurnIdentity("request-turn", 1),
        )
    )

    assert result["state"] == "awaiting_user"
    assert result["access_granted"] is False
    assert result["next"] == "user_decision"
    assert "lease" not in result
    assert "id" not in result
    assert "revision" not in result
    assert "application_hash" not in result


@pytest.mark.asyncio
async def test_request_host_access_tool_reports_automatic_activation(
    isolated_db,
    monkeypatch,
):
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def activated(_thread_id, **_application):
        return {"state": "active"}

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", activated)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Inspect processes"],
            duration_seconds=60,
            _thread_id="root",
        )
    )

    assert result == {
        "state": "active",
        "access_granted": True,
        "next": "lease_active",
    }


@pytest.mark.asyncio
async def test_request_host_access_tool_without_context_fails_closed(isolated_db):
    from tools.host_access import RequestHostAccessTool

    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Inspect processes"],
            duration_seconds=60,
        )
    )

    assert result == {
        "error": "host_access_request_unattributed",
        "access_granted": False,
    }


@pytest.mark.asyncio
async def test_request_host_access_tool_tells_agent_to_rerequest_changed_policy(
    isolated_db,
    monkeypatch,
):
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def changed(_thread_id, **_application):
        return {
            "state": "blocked",
            "terminal_reason": "project_policy_changed",
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", changed)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Repair the checkout",
            plan=["Apply the repair"],
            duration_seconds=60,
            _thread_id="root",
        )
    )

    assert result == {
        "state": "blocked",
        "access_granted": False,
        "reason": "project_policy_changed",
        "next": "project_policy_changed_re_request_host_access",
    }


@pytest.mark.asyncio
async def test_request_host_access_tool_tells_agent_to_rerequest_expired_lease(
    isolated_db,
    monkeypatch,
):
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def expired(_thread_id, **_application):
        return {
            "state": "expired",
            "terminal_reason": "lease_expired",
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", expired)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
        )
    )

    assert result == {
        "state": "expired",
        "access_granted": False,
        "reason": "lease_expired",
        "next": "lease_expired_re_request",
    }


@pytest.mark.asyncio
async def test_request_host_access_tool_tells_agent_to_apply_for_new_scope_revoked(
    isolated_db,
    monkeypatch,
):
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def revoked(_thread_id, **_application):
        return {
            "state": "revoked",
            "terminal_reason": "user_revoked",
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", revoked)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
        )
    )

    assert result == {
        "state": "revoked",
        "access_granted": False,
        "reason": "user_revoked",
        "next": "request_host_access_for_new_scope",
    }


@pytest.mark.asyncio
async def test_request_host_access_tool_withholds_an_unaudited_reason(
    isolated_db,
    monkeypatch,
):
    """The reason gate is fail-closed on the producer, not the lease state.

    A reason no known producer writes has never been audited for third-party
    text, so it is withheld even though the state would otherwise carry one.
    The specific next step still stands.
    """
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def unaudited(_thread_id, **_application):
        return {
            "state": "blocked",
            # The review stage with an unrecognised suffix: a prefix match
            # would forward the provider text and the key with it.
            "terminal_reason": (
                "application_reviewer_failure: https://provider/err?key=sk-secret"
            ),
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", unaudited)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
        )
    )

    assert result == {
        "state": "blocked",
        "access_granted": False,
        "next": "application_review_failed",
    }


def test_request_host_access_description_covers_both_approval_modes():
    from tools.host_access import RequestHostAccessTool

    description = RequestHostAccessTool().description
    assert "activates the lease immediately" in description
    assert "one user decision" in description


@pytest.mark.asyncio
async def test_release_host_access_tool_reports_the_released_lease(
    isolated_db,
    monkeypatch,
):
    from tools import host_access as host_access_tool
    from tools.host_access import ReleaseHostAccessTool

    captured: list[str] = []

    async def release(thread_id):
        captured.append(thread_id)
        return {"state": "revoked", "terminal_reason": "agent_released"}

    monkeypatch.setattr(host_access_tool, "release_requested_lease", release)
    result = json.loads(
        await ReleaseHostAccessTool().execute(_thread_id="root")
    )

    assert captured == ["root"]
    assert result == {
        "state": "revoked",
        "released": True,
        "next": "request_host_access_for_new_scope",
    }


@pytest.mark.asyncio
async def test_release_host_access_tool_reports_no_open_lease(
    isolated_db,
    monkeypatch,
):
    from tools import host_access as host_access_tool
    from tools.host_access import ReleaseHostAccessTool

    async def release(_thread_id):
        return None

    monkeypatch.setattr(host_access_tool, "release_requested_lease", release)
    result = json.loads(
        await ReleaseHostAccessTool().execute(_thread_id="root")
    )

    assert result == {"released": False, "next": "no_open_lease"}


@pytest.mark.asyncio
async def test_release_host_access_tool_without_context_fails_closed(isolated_db):
    from tools.host_access import ReleaseHostAccessTool

    result = json.loads(await ReleaseHostAccessTool().execute())

    assert result == {
        "error": "host_access_release_unattributed",
        "released": False,
    }


def test_release_host_access_public_schema_takes_no_arguments():
    from tools.host_access import ReleaseHostAccessTool

    tool = ReleaseHostAccessTool()
    assert tool.name == "release_host_access"
    assert tool.parameters == {
        "type": "object",
        "required": [],
        "additionalProperties": False,
        "properties": {},
    }
    assert "release" in tool.description.lower()
    assert "request_host_access" in tool.description


def test_request_host_access_public_schema_is_bounded():
    from infra.host_access_leases import (
        MAX_LEASE_DURATION_SECONDS,
        MAX_PLAN_STEP_CHARS,
        MAX_PLAN_STEPS,
        MAX_PURPOSE_CHARS,
        MAX_RISK_FIELD_CHARS,
        MAX_POLICY_EXCEPTION_REASON_CHARS,
        MAX_POLICY_EXCEPTION_REPO_NAME_CHARS,
        MAX_POLICY_EXCEPTIONS,
    )
    from tools.host_access import RequestHostAccessTool

    schema = RequestHostAccessTool().parameters
    assert schema["required"] == ["purpose", "plan", "duration_seconds"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["duration_seconds"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_LEASE_DURATION_SECONDS,
        "description": "Requested lease duration in seconds (maximum 30 minutes).",
    }
    assert "_thread_id" not in schema["properties"]
    assert schema["properties"]["purpose"]["maxLength"] == MAX_PURPOSE_CHARS
    assert schema["properties"]["plan"]["maxItems"] == MAX_PLAN_STEPS
    assert (
        schema["properties"]["plan"]["items"]["maxLength"]
        == MAX_PLAN_STEP_CHARS
    )
    for field in (
        "expected_resources",
        "persistent_effects",
        "rollback_plan",
        "forbidden_effects",
    ):
        assert schema["properties"][field]["maxLength"] == MAX_RISK_FIELD_CHARS
    exception_schema = schema["properties"]["requested_policy_exceptions"]
    assert exception_schema["type"] == "array"
    assert exception_schema["maxItems"] == MAX_POLICY_EXCEPTIONS
    repo_schema, external_schema = exception_schema["items"]["oneOf"]
    assert repo_schema["required"] == ["repo_name", "policy", "reason"]
    assert repo_schema["properties"]["policy"]["enum"] == [
        "branch_pinning",
        "protected_base_checkout",
    ]
    assert repo_schema["properties"]["repo_name"]["maxLength"] == (
        MAX_POLICY_EXCEPTION_REPO_NAME_CHARS
    )
    assert external_schema["required"] == [
        "policy",
        "reason",
        "command",
        "working_directory",
    ]
    assert external_schema["additionalProperties"] is False
    assert external_schema["properties"]["policy"] == {"const": "external_agent"}
    assert external_schema["properties"]["command"]["minLength"] == 1
    assert external_schema["properties"]["working_directory"]["minLength"] == 1


def test_external_agent_guidance_names_parent_parking_and_no_timeout_approval():
    from tools.host_access import RequestHostAccessTool

    description = RequestHostAccessTool().description
    assert "release_host_access({})" in description
    assert "this thread's own request" in description
    assert "awaiting_user" in description
    assert "An application belongs to the thread that files it" in description
    assert "a delegated child may file its own" in description
    assert "parks on its parent" in description
    assert "never release a request it inherited from a parent" in description
    assert "host_access.activated" in description
    assert "bounded fallback" in description
    assert "timeout never grants approval" in description
    # The child owns its own application; nothing here may imply it cannot file.
    assert "never files its own" not in description
    assert "child cannot request" not in description


@pytest.mark.asyncio
async def test_request_host_access_tool_reports_why_the_review_failed(
    isolated_db,
    monkeypatch,
):
    """A blocked lease must say which correction applies, not just that it failed."""
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def blocked_request(thread_id, **application):
        # Seeded with the identifiers the projection must drop, so the
        # assertions below can actually fail if it stops dropping them.
        return {
            "id": "lease-abc",
            "revision": 3,
            "application_hash": "hash-xyz",
            "state": "blocked",
            "terminal_reason": "application_reviewer_failure: rate_limit",
        }

    monkeypatch.setattr(
        host_access_tool,
        "request_reviewed_lease",
        blocked_request,
    )
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
            _host_access_turn_identity=TurnIdentity("request-turn", 1),
        )
    )

    assert result["state"] == "blocked"
    assert result["access_granted"] is False
    assert result["next"] == "application_review_failed"
    # The bounded cause an agent acts on, not a provider message or class name.
    assert result["reason"] == "application_reviewer_failure: rate_limit"
    # Still no lease identifiers, proven against a lease that carries them.
    assert "id" not in result
    assert "revision" not in result
    assert "application_hash" not in result


@pytest.mark.asyncio
async def test_request_host_access_tool_names_the_next_step_for_an_account_cause(
    isolated_db,
    monkeypatch,
):
    """An account-admission refusal carries its fixed correction, not a bare cause."""
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def account_blocked(_thread_id, **_application):
        return {
            "state": "blocked",
            "terminal_reason": "application_reviewer_failure: reviewer_account_unavailable",
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", account_blocked)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
        )
    )

    assert result["reason"] == "application_reviewer_failure: reviewer_account_unavailable"
    assert "reviewer" in result["guidance"] and "account" in result["guidance"]


@pytest.mark.asyncio
async def test_request_host_access_tool_omits_reason_when_nothing_ended(
    isolated_db,
    monkeypatch,
):
    """A live lease has no terminal reason, so the key must be absent."""
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def pending_request(thread_id, **application):
        return {"state": "awaiting_user", "terminal_reason": None}

    monkeypatch.setattr(
        host_access_tool,
        "request_reviewed_lease",
        pending_request,
    )
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
            _host_access_turn_identity=TurnIdentity("request-turn", 1),
        )
    )

    assert result["next"] == "user_decision"
    assert "reason" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_reason",
    ["activation_failed", "browser_isolation_failed"],
)
async def test_request_host_access_tool_post_verdict_failure_says_its_stage(
    isolated_db,
    monkeypatch,
    terminal_reason,
):
    """A post-verdict failure must not be labelled a review failure.

    The whole point of surfacing `reason` is choosing the right correction;
    `next` contradicting it would recreate the ambiguity this change removes.
    """
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def post_verdict_failure(_thread_id, **_application):
        return {
            "state": "blocked",
            "terminal_reason": terminal_reason,
        }

    monkeypatch.setattr(
        host_access_tool,
        "request_reviewed_lease",
        post_verdict_failure,
    )
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
            _host_access_turn_identity=TurnIdentity("request-turn", 1),
        )
    )

    assert result["state"] == "blocked"
    assert result["access_granted"] is False
    assert result["next"] == "lease_activation_failed"
    assert result["reason"] == terminal_reason


@pytest.mark.asyncio
async def test_request_host_access_tool_never_leaks_reviewer_verdict_reasoning(
    isolated_db,
    monkeypatch,
):
    """A rejected lease's terminal_reason must never surface as `reason`.

    The reviewer's verdict reasoning is not a lifecycle fact, so the tool
    gates `reason` on the lifecycle states whose terminal_reason is safe to
    expose. A rejected lease carrying a terminal_reason (which would only
    happen after a projection change) must still omit it.
    """
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def rejected_request(_thread_id, **_application):
        return {
            "state": "rejected",
            "terminal_reason": "rejected: reviewer verdict detail",
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", rejected_request)
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
            _host_access_turn_identity=TurnIdentity("request-turn", 1),
        )
    )

    assert result["state"] == "rejected"
    assert result["access_granted"] is False
    assert result["next"] == "application_rejected"
    assert "reason" not in result


@pytest.mark.asyncio
async def test_request_host_access_tool_blocked_without_reason_keeps_review_next(
    isolated_db,
    monkeypatch,
):
    """A blocked lease with no terminal_reason still says the review failed.

    Blocks recorded before this change are durable and may carry no reason,
    and a future _safe_block reason outside the stage prefixes must not
    silently degrade the agent-facing guidance to lease_state_changed.
    """
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def blocked_no_reason(_thread_id, **_application):
        return {"state": "blocked", "terminal_reason": None}

    monkeypatch.setattr(
        host_access_tool,
        "request_reviewed_lease",
        blocked_no_reason,
    )
    result = json.loads(
        await RequestHostAccessTool().execute(
            purpose="Inspect the local runtime",
            plan=["Read logs"],
            duration_seconds=60,
            _thread_id="root",
            _host_access_turn_identity=TurnIdentity("request-turn", 1),
        )
    )

    assert result["state"] == "blocked"
    assert result["next"] == "application_review_failed"
    assert "reason" not in result


@pytest.mark.asyncio
async def test_rejected_application_shows_safe_category_not_reviewer_text(monkeypatch):
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def rejected(_thread_id, **_application):
        return {
            "state": "rejected",
            "terminal_reason": "application_review_rejected",
            "application_review_result": {
                "reason_category": "unclear_scope",
                "reason": "Unsafe copied secret: EXAMPLE-SENSITIVE-VALUE",
            },
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", rejected)
    result = json.loads(await RequestHostAccessTool().execute(
        purpose="Inspect", plan=["Read"], duration_seconds=60, _thread_id="root"
    ))
    assert result["next"] == "application_rejected"
    assert result["reason"] == "application_review_rejected"
    assert result["reason_category"] == "unclear_scope"
    assert "exact resources" in result["guidance"]
    assert "EXAMPLE-SENSITIVE-VALUE" not in json.dumps(result)


def test_every_rejection_category_has_fixed_guidance():
    from host_access.failure_reasons import REJECTION_CATEGORIES, REJECTION_GUIDANCE

    assert set(REJECTION_GUIDANCE) == REJECTION_CATEGORIES | {"unknown"}


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("unclear_purpose", "user-authorized outcome"),
        ("unclear_resources", "exact host files"),
        ("unclear_actions", "ordered actions"),
        ("unclear_effects", "persistent_effects"),
    ],
)
def test_scope_rejection_identifies_the_field_to_repair(category, expected):
    from host_access.failure_reasons import safe_rejection

    actual_category, guidance = safe_rejection(category)
    assert actual_category == category
    assert expected in guidance
    assert "private review" not in guidance


@pytest.mark.asyncio
async def test_legacy_rejection_uses_unknown_category_and_fixed_guidance(monkeypatch):
    from tools import host_access as host_access_tool
    from tools.host_access import RequestHostAccessTool

    async def legacy_rejection(_thread_id, **_application):
        return {
            "state": "rejected",
            "terminal_reason": "application_review_rejected",
            "application_review_result": {"reason": "PRIVATE-REVIEW-TEXT"},
        }

    monkeypatch.setattr(host_access_tool, "request_reviewed_lease", legacy_rejection)
    result = json.loads(await RequestHostAccessTool().execute(
        purpose="Inspect", plan=["Read"], duration_seconds=60, _thread_id="root"
    ))
    assert result["reason"] == "application_review_rejected"
    assert result["reason_category"] == "unknown"
    assert "unavailable" in result["guidance"]
    assert "PRIVATE-REVIEW-TEXT" not in json.dumps(result)

