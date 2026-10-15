"""Host-access reviewer fallback: the requester's model reviews when the chosen one cannot run."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

RESET = "2026-10-14T08:08:00+00:00"
OUT = "claude-code/opus"
REQUESTER = "claude-code/sonnet"


def _until(value: str = RESET) -> str:
    moment = datetime.fromisoformat(value).astimezone()
    return f"{moment:%b} {moment.day}, {moment.hour % 12 or 12}:{moment:%M %p %Z}"


def _configure(*, fallback: bool, reviewer: str | None = OUT) -> None:
    from infra.settings import set_setting

    defaults: dict = {"host_access_reviewer_fallback": fallback}
    if reviewer is not None:
        defaults["host_access_reviewer_model"] = reviewer
    set_setting("agents", {"defaults": defaults})


def _insert_thread(thread_id: str, model: str | None = REQUESTER) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES (?,?,?,?,'t','t')",
            (thread_id, "steve", thread_id, model),
        )
        db.commit()
    finally:
        db.close()


class _Outage:
    """Which catalog models the account decision currently refuses, and why."""

    def __init__(self, monkeypatch) -> None:
        from reviewer import reviewer_account

        self.models: dict[str, tuple[str, str | None]] = {}
        self.calls: list[str] = []
        monkeypatch.setattr(reviewer_account, "resolve_scope_reviewer_account", self._resolve)

    def _resolve(self, target, project_id, *, db):
        from reviewer.failures import CCUnavailableError

        self.calls.append(target.catalog_id)
        if target.catalog_id in self.models:
            reason, resets_at = self.models[target.catalog_id]
            error = CCUnavailableError(f"Reviewer account unavailable: {reason}")
            error.reason = reason
            error.resets_at = resets_at
            raise error
        return "account-1"


@pytest.fixture
def outage(isolated_db, monkeypatch) -> _Outage:
    _insert_thread("root")
    return _Outage(monkeypatch)


async def _resolve(expected: str | None = None):
    from host_access.reviewer_target import resolve_reviewer_target

    return await resolve_reviewer_target(expected, requester_thread_id="root")


@pytest.mark.asyncio
async def test_fallback_on_reviews_with_the_requester_model_then_returns_to_the_chosen_one(outage):
    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)

    during = await _resolve()
    outage.models.clear()
    after = await _resolve()

    assert during.catalog_id == REQUESTER
    assert during.reviewer_fallback is True
    assert after.catalog_id == OUT
    assert getattr(after, "reviewer_fallback", False) is False


@pytest.mark.asyncio
async def test_fallback_off_refuses_with_the_cause_and_reset_then_recovers(outage):
    from host_access.errors import ReviewerUnavailableError

    _configure(fallback=False)
    outage.models[OUT] = ("usage_limited", RESET)

    with pytest.raises(ReviewerUnavailableError) as refused:
        await _resolve()
    outage.models.clear()
    recovered = await _resolve()

    error = refused.value
    assert str(error) == "host-access reviewer account unavailable: usage_limited"
    assert (error.outage.reason, error.outage.resets_at) == ("usage_limited", RESET)
    assert (error.pinned, error.fallback_enabled, error.fallback) == (False, False, None)
    assert REQUESTER not in outage.calls
    assert recovered.catalog_id == OUT


@pytest.mark.asyncio
async def test_a_pinned_review_never_falls_back(outage):
    from host_access.errors import ReviewerUnavailableError

    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)

    with pytest.raises(ReviewerUnavailableError) as refused:
        await _resolve(OUT)

    assert (refused.value.pinned, refused.value.fallback_enabled) == (True, True)
    assert outage.calls == [OUT]


@pytest.mark.asyncio
async def test_both_models_out_names_both(outage):
    from host_access.errors import ReviewerUnavailableError

    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)
    outage.models[REQUESTER] = ("account_not_configured", None)

    with pytest.raises(ReviewerUnavailableError) as refused:
        await _resolve()

    assert refused.value.outage.reason == "usage_limited"
    assert refused.value.fallback.reason == "account_not_configured"
    assert refused.value.fallback_enabled is True


@pytest.mark.asyncio
async def test_a_model_the_catalog_marks_unavailable_counts_as_cannot_run(outage, monkeypatch):
    import llm.resolution as resolution
    from dataclasses import replace
    from host_access.errors import ReviewerUnavailableError

    _configure(fallback=False)
    real = resolution.resolve_execution_target

    def unavailable(model, **kwargs):
        target = real(model, **kwargs)
        return replace(target, available=False, availability_reason="not_discovered")

    monkeypatch.setattr(resolution, "resolve_execution_target", unavailable)

    with pytest.raises(ReviewerUnavailableError) as refused:
        await _resolve()

    assert str(refused.value) == "host-access reviewer model is unavailable"
    assert refused.value.outage.reason == "not_discovered"


@pytest.mark.asyncio
async def test_with_no_chosen_reviewer_the_requesters_own_outage_is_an_ordinary_failure(outage):
    from host_access.errors import HostAccessSupervisorError, ReviewerUnavailableError

    _configure(fallback=True, reviewer=None)
    outage.models[REQUESTER] = ("usage_limited", RESET)

    with pytest.raises(HostAccessSupervisorError) as refused:
        await _resolve()

    assert not isinstance(refused.value, ReviewerUnavailableError)


@pytest.mark.asyncio
async def test_fallback_without_a_requester_model_refuses_as_if_off(isolated_db, monkeypatch):
    from host_access.errors import ReviewerUnavailableError

    _insert_thread("root", model=None)
    outage = _Outage(monkeypatch)
    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)

    with pytest.raises(ReviewerUnavailableError) as refused:
        await _resolve()

    assert refused.value.fallback is None


# --- supervisor and tool ---------------------------------------------------


def _verdict_capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake(target, **kwargs):
        calls.append({"target": target, **kwargs})
        return kwargs["validator"]({
            "schema_version": 3,
            "verdict": "allow",
            "reason_category": "not_applicable",
            "reason": "The request is bounded.",
            "user_attestation": "This grants a short, reviewed session.",
        })

    monkeypatch.setattr("host_access.reviewer.invoke_isolated_structured", fake)
    return calls


async def _request(**overrides) -> dict:
    from host_access import supervisor

    return await supervisor.request_reviewed_lease(
        "root", purpose="Inspect host state", plan=["Read process state"],
        duration_seconds=60, **overrides,
    )


@pytest.mark.asyncio
async def test_fallback_review_runs_on_the_requester_model_and_records_it(outage, monkeypatch):
    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)
    calls = _verdict_capture(monkeypatch)

    lease = await _request()

    assert [call["target"].catalog_id for call in calls] == [REQUESTER]
    assert lease["state"] == "awaiting_user"
    assert lease["reviewer_model"] == REQUESTER
    assert lease["application_review_result"]["reviewer_fallback"] is True


@pytest.mark.asyncio
async def test_a_normal_review_records_no_fallback(outage, monkeypatch):
    _configure(fallback=True)
    calls = _verdict_capture(monkeypatch)

    lease = await _request()

    assert [call["target"].catalog_id for call in calls] == [OUT]
    assert lease["application_review_result"]["reviewer_fallback"] is False


@pytest.mark.asyncio
async def test_a_reviewer_that_ran_and_failed_does_not_fall_back(outage, monkeypatch):
    from llm.isolated_structured import IsolatedStructuredProviderError

    _configure(fallback=True)
    calls: list[str] = []

    async def failing(target, **kwargs):
        calls.append(target.catalog_id)
        raise IsolatedStructuredProviderError("provider failed")

    monkeypatch.setattr("host_access.reviewer.invoke_isolated_structured", failing)

    lease = await _request()

    assert calls == [OUT]
    assert lease["state"] == "blocked"
    assert lease["terminal_reason"] == "application_reviewer_failure: provider_error"


def _product_events(monkeypatch) -> list[tuple[str, str, dict]]:
    events: list[tuple[str, str, dict]] = []

    def capture(feature, action, **kwargs):
        events.append((feature, action, kwargs.get("properties")))

    monkeypatch.setattr("host_access.reviewer_target.emit_product_event", capture)
    monkeypatch.setattr("host_access.review_telemetry.emit_product_event", capture)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [True, False])
async def test_a_reviewer_that_cannot_run_is_reported_with_its_cause_and_the_setting(outage, monkeypatch, fallback):
    from host_access.errors import ReviewerUnavailableError

    events = _product_events(monkeypatch)
    _configure(fallback=fallback)
    outage.models[OUT] = ("usage_limited", RESET)

    if fallback:
        assert (await _resolve()).reviewer_fallback is True
    else:
        with pytest.raises(ReviewerUnavailableError):
            await _resolve()

    assert events == [("host_access_review", "reviewer_unavailable", {
        "cause": "usage_limited", "fallback_enabled": fallback, "pinned": False, "named": True,
    })]


@pytest.mark.asyncio
async def test_a_reviewer_that_can_run_reports_nothing_unavailable(outage, monkeypatch):
    events = _product_events(monkeypatch)
    _configure(fallback=True)

    await _resolve()

    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("out", [True, False])
async def test_the_completed_review_event_says_whether_the_fallback_reviewed(outage, monkeypatch, out):
    from llm.isolated_structured import IsolatedStructuredTelemetry

    events = _product_events(monkeypatch)
    _configure(fallback=True)
    if out:
        outage.models[OUT] = ("usage_limited", RESET)

    async def fake(target, **kwargs):
        kwargs["telemetry_sink"](IsolatedStructuredTelemetry(
            catalog_id=target.catalog_id, harness=target.harness, transport_model=target.transport_model,
            reported_model=target.expected_reported_model, prompt_tokens=None, completion_tokens=None,
            cache_creation_tokens=None, cache_read_tokens=None, total_tokens=None, cost_usd=None,
            cost_source=None, elapsed_ms=3, usage_available=False,
        ))
        return kwargs["validator"]({
            "schema_version": 3,
            "verdict": "allow",
            "reason_category": "not_applicable",
            "reason": "The request is bounded.",
            "user_attestation": "This grants a short, reviewed session.",
        })

    monkeypatch.setattr("host_access.reviewer.invoke_isolated_structured", fake)

    await _request()

    completed = [props for _feature, action, props in events if action == "completed"]
    assert [(props["catalog_id"], props["reviewer_fallback"]) for props in completed] == [
        (REQUESTER if out else OUT, out),
    ]


def _display_name(model: str) -> str:
    from infra.database import get_db
    from llm.resolution import resolve_execution_target

    db = get_db()
    try:
        return resolve_execution_target(model, db=db).display_name
    finally:
        db.close()


def _tool_result(result: str) -> dict:
    return json.loads(result)


async def _tool_call() -> dict:
    from tools.host_access import RequestHostAccessTool

    return _tool_result(await RequestHostAccessTool().execute(
        _thread_id="root", purpose="Inspect host state",
        plan=["Read process state"], duration_seconds=60,
    ))


@pytest.mark.asyncio
async def test_fallback_off_tells_the_agent_why_and_what_to_do(outage, monkeypatch):
    _configure(fallback=False)
    outage.models[OUT] = ("usage_limited", RESET)
    calls = _verdict_capture(monkeypatch)

    result = await _tool_call()

    until = _until()
    assert calls == []
    assert result["state"] == "blocked"
    assert result["next"] == "application_review_failed"
    assert result["reason"] == "reviewer_unavailable"
    assert result["guidance"] == (
        "The host-access reviewer chosen in Settings can't run right now "
        f"(usage limit until {until}). Your application was not reviewed. "
        "Tell the person once that host-access review is down; they can choose "
        "another model or turn on fallback in Settings > System > Host Access "
        f"Review. Set the task aside until {until}, or until the reviewer is "
        "back, and continue other work."
    )


@pytest.mark.asyncio
async def test_both_models_out_guidance_names_both(outage, monkeypatch):
    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)
    outage.models[REQUESTER] = ("usage_limited", "2026-10-13T08:08:00+00:00")
    _verdict_capture(monkeypatch)

    result = await _tool_call()

    names = {model: _display_name(model) for model in (OUT, REQUESTER)}
    assert result["reason"] == "reviewer_unavailable"
    assert result["guidance"].startswith(
        "Neither the host-access reviewer chosen in Settings "
        f"({names[OUT]}: usage limit until {_until()}) nor the requesting "
        f"agent's own model ({names[REQUESTER]}: usage limit until "
        f"{_until('2026-10-13T08:08:00+00:00')}) can run right now. "
        "Your application was not reviewed."
    )
    assert f"Set the task aside until {_until('2026-10-13T08:08:00+00:00')}," in result["guidance"]


@pytest.mark.asyncio
async def test_guidance_reports_the_cause_of_the_failure_even_if_the_outage_clears_before_it_is_built(
    outage, monkeypatch,
):
    from host_access import supervisor

    _configure(fallback=False)
    outage.models[OUT] = ("usage_limited", RESET)
    _verdict_capture(monkeypatch)
    real_block = supervisor._safe_block

    async def block_then_recover(lease_id, reason, **kwargs):
        blocked = await real_block(lease_id, reason, **kwargs)
        outage.models.clear()  # the reviewer is back by the time the agent is told
        return blocked

    monkeypatch.setattr(supervisor, "_safe_block", block_then_recover)

    result = await _tool_call()

    assert result["reason"] == "reviewer_unavailable"
    assert f"(usage limit until {_until()})" in result["guidance"]


@pytest.mark.asyncio
async def test_an_outage_without_a_reset_time_says_wait_for_the_reviewer(outage, monkeypatch):
    _configure(fallback=False)
    outage.models[OUT] = ("account_not_configured", None)
    _verdict_capture(monkeypatch)

    result = await _tool_call()

    assert result["guidance"].startswith(
        "The host-access reviewer chosen in Settings can't run right now "
        "(account not configured). Your application was not reviewed."
    )
    assert result["guidance"].endswith(
        "Set the task aside until the reviewer is back, and continue other work."
    )


# --- action refusals --------------------------------------------------------


async def _action_refusal() -> str:
    from host_access import supervisor
    from host_access.reviewer_unavailable import action_review_refusal

    with pytest.raises(supervisor.ActionReviewUnavailableError) as refused:
        await supervisor._reviewer_target_or_unavailable(OUT, "root")
    return action_review_refusal(refused.value)


@pytest.mark.asyncio
async def test_pinned_action_refusal_with_fallback_on_says_apply_again(outage):
    _configure(fallback=True)
    outage.models[OUT] = ("usage_limited", RESET)

    assert await _action_refusal() == (
        "This lease's reviewer can't run. Apply again; with fallback on, your own "
        "model will review the new application."
    )
    assert outage.calls == [OUT]


@pytest.mark.asyncio
async def test_pinned_action_refusal_with_fallback_off_names_the_cause(outage):
    _configure(fallback=False)
    outage.models[OUT] = ("usage_limited", RESET)

    until = _until()
    assert await _action_refusal() == (
        "The host-access reviewer chosen in Settings can't run right now "
        f"(usage limit until {until}). Your action was not reviewed. "
        "Tell the person once that host-access review is down; they can choose "
        "another model or turn on fallback in Settings > System > Host Access "
        f"Review. Set the task aside until {until}, or until the reviewer is "
        "back, and continue other work."
    )


@pytest.mark.asyncio
async def test_an_action_refusal_that_is_not_an_outage_keeps_its_wording(isolated_db):
    from host_access.reviewer_unavailable import action_review_refusal
    from host_access.supervisor import ActionReviewUnavailableError

    refusal = action_review_refusal(
        ActionReviewUnavailableError("host-access action review did not complete: timeout")
    )

    assert refusal == (
        "Host action denied: host-access action review did not complete: timeout. "
        "Your lease is still active; retrying the action triggers a fresh review."
    )


def test_the_reviewer_unavailable_reason_is_holder_safe():
    from host_access.failure_reasons import is_holder_safe

    assert is_holder_safe("reviewer_unavailable")
