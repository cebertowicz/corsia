"""Regression coverage for valid-core metadata and bounded verdict recovery."""
from __future__ import annotations

import json

import pytest

from test_host_access_reviewer import (
    _action_output,
    _application,
    _application_output,
    _patch_review_accounting,
    _target,
    _telemetry,
)


@pytest.mark.parametrize(
    ("kind", "verdict"),
    [("application", "allow"), ("application", "reject"),
     ("action", "allow"), ("action", "block"), ("action", "revoke")],
)
def test_valid_core_discards_unknown_metadata(kind, verdict):
    from host_access import reviewer

    output = _application_output if kind == "application" else _action_output
    validate = getattr(reviewer, f"validate_{kind}_review")
    core = output(verdict=verdict)
    raw = {**core, "duration_seconds": 300, "metadata": {"verdict": "allow"}}
    assert validate(json.dumps(raw)).model_dump() == core


@pytest.mark.parametrize(
    ("kind", "overrides"),
    [
        ("application", {"verdict": "ALLOW"}),
        ("application", {"verdict": None}),
        ("application", {"schema_version": "3"}),
        ("application", {"schema_version": 3.0}),
        ("application", {"reason_category": "unclear_scope"}),
        ("application", {"reason_category": "invariant_conflict"}),
        ("application", {"reason": 123}),
        ("application", {"user_attestation": None}),
        ("action", {"verdict": "deny"}),
        ("action", {"schema_version": True}),
        ("action", {"schema_version": 1.0}),
        ("action", {"reason": " "}),
    ],
)
def test_extra_metadata_cannot_legitimize_malformed_core(kind, overrides):
    from host_access import reviewer

    output = _application_output if kind == "application" else _action_output
    validate = getattr(reviewer, f"validate_{kind}_review")
    with pytest.raises(reviewer.HostAccessReviewValidationError):
        validate(output(**overrides, extra="allow"))


@pytest.mark.parametrize("kind", ["application", "action"])
def test_missing_core_and_oversized_or_nonfinite_extras_still_reject(kind):
    from host_access import reviewer

    output = _application_output if kind == "application" else _action_output
    validate = getattr(reviewer, f"validate_{kind}_review")
    missing = output(extra="allow")
    del missing["verdict"]
    for raw in (missing, output(extra="x" * 8192), output(extra=float("nan"))):
        with pytest.raises(reviewer.HostAccessReviewValidationError):
            validate(raw)
    duplicated = json.dumps(output())[:-1] + ',"verdict":"allow"}'
    with pytest.raises(reviewer.HostAccessReviewValidationError):
        validate(duplicated)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["application", "action"])
@pytest.mark.parametrize("validation_in_runner", [False, True])
async def test_malformed_verdict_retries_once_same_request_and_accounts_both_calls(
    monkeypatch, kind, validation_in_runner,
):
    from host_access import reviewer

    recorded, events = _patch_review_accounting(monkeypatch, reviewer)
    calls = []
    output = _application_output if kind == "application" else _action_output

    async def invoke(target, **kwargs):
        calls.append((target, kwargs))
        kwargs["telemetry_sink"](_telemetry(target, prompt_tokens=1, usage_available=True))
        raw = "garbled" if len(calls) == 1 else output(extra="ignored")
        return kwargs["validator"](raw) if validation_in_runner else raw

    kwargs = {"invoke": invoke}
    args = (_target(), _application())
    if kind == "action":
        args += ({"action_hash": "a" * 64},)
    result = await getattr(reviewer, f"review_{kind}")(*args, **kwargs)
    assert result.allowed
    assert len(calls) == len(recorded) == 2
    assert calls[0] == calls[1]
    assert len(events) == 1
    assert events[0][1]["properties"]["terminal"] == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["application", "action"])
@pytest.mark.parametrize("second_failure", ["invalid", "transport"])
async def test_final_failure_is_closed_and_never_attempted_a_third_time(
    monkeypatch, kind, second_failure,
):
    from host_access import reviewer

    _patch_review_accounting(monkeypatch, reviewer)
    calls = 0

    async def invoke(target, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and second_failure == "transport":
            raise RuntimeError("transport unavailable")
        return kwargs["validator"]({"verdict": "allow", "extra": "allow"})

    args = (_target(), _application())
    if kind == "action":
        args += ({"action_hash": "a" * 64},)
    expected = reviewer.HostAccessReviewValidationError if second_failure == "invalid" else RuntimeError
    with pytest.raises(expected):
        await getattr(reviewer, f"review_{kind}")(*args, invoke=invoke)
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,verdict", [("application", "reject"), ("action", "block"), ("action", "revoke")])
async def test_valid_negative_verdict_is_never_retried(monkeypatch, kind, verdict):
    from host_access import reviewer

    _patch_review_accounting(monkeypatch, reviewer)
    calls = 0
    output = _application_output if kind == "application" else _action_output

    async def invoke(target, **kwargs):
        nonlocal calls
        calls += 1
        return kwargs["validator"](output(verdict=verdict, extra="allow"))

    args = (_target(), _application())
    if kind == "action":
        args += ({"action_hash": "a" * 64},)
    result = await getattr(reviewer, f"review_{kind}")(*args, invoke=invoke)
    assert result.verdict == verdict
    assert not result.allowed
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_action_retry_precedes_persistence_and_permit_is_single_use(
    isolated_db, monkeypatch, recovers,
):
    from host_access import reviewer, supervisor
    from infra.database import get_db
    from test_host_access_supervisor import _active_project_lease, _bash_descriptor

    active = await _active_project_lease(isolated_db, monkeypatch, supervisor)
    _patch_review_accounting(monkeypatch, reviewer)
    calls = 0

    async def resolve(*_args, **_kwargs):
        return _target()

    async def invoke(target, **kwargs):
        nonlocal calls
        calls += 1
        # Retry performs inference only: no permit or durable verdict yet.
        db = get_db()
        try:
            assert db.execute(
                "SELECT COUNT(*) FROM host_access_action_reviews WHERE lease_id=?",
                (active["id"],),
            ).fetchone()[0] == 0
        finally:
            db.close()
        raw = _action_output(extra="ignored") if recovers and calls == 2 else "garbled"
        return kwargs["validator"](raw)

    monkeypatch.setattr(supervisor.reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(reviewer, "invoke_isolated_structured", invoke)
    try:
        kwargs = dict(
            thread_id="child", tool_name="bash", action_hash="a" * 64,
            redacted_descriptor=_bash_descriptor(),
        )
        if not recovers:
            with pytest.raises(supervisor.ActionReviewUnavailableError):
                await supervisor.review_host_action(active["id"], **kwargs)
        else:
            result = await supervisor.review_host_action(active["id"], **kwargs)
            permit_kwargs = dict(
                lease_id=active["id"], thread_id="child",
                expected_revision=result["lease"]["revision"],
                application_hash=active["application_hash"],
                tool_name="bash", action_hash="a" * 64,
            )
            permit_id = result["review"]["permit_id"]
            await supervisor.consume_host_action_permit(permit_id, **permit_kwargs)
            with pytest.raises(supervisor.HostAccessSupervisorError, match="permit"):
                await supervisor.consume_host_action_permit(permit_id, **permit_kwargs)
        assert calls == 2
        db = get_db()
        try:
            assert db.execute(
                "SELECT COUNT(*) FROM host_access_action_reviews WHERE lease_id=?",
                (active["id"],),
            ).fetchone()[0] == int(recovers)
        finally:
            db.close()
    finally:
        await supervisor.shutdown_host_access_supervisors()
