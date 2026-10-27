"""Check the admitted delivery path through the real webhook scheduler and router."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from events import ingress, router
from events.connectors import github_admission as admission
from events.connectors import github_head_checks as checks
from events.connectors import github_webhook as webhook
from infra import drain


@pytest.mark.asyncio
async def test_admission_routes_only_new_completed_suites(monkeypatch):
    seen = []
    terminal = admission.AdmissionOutcome(200, {"status": "held_or_duplicate"})
    monkeypatch.setattr(admission, "_record_phase", lambda _t, _p, d, _s: (terminal if d in {"dup", "held"} else None, d))
    monkeypatch.setattr(admission, "_settle_phase", lambda *_args: admission.AdmissionOutcome(200, {}))
    monkeypatch.setattr(drain, "begin_turn_admission", lambda: object())
    monkeypatch.setattr(drain, "finish_turn_admission", lambda _admission: None)
    monkeypatch.setattr(drain, "inherit_turn_admission", lambda _admission: nullcontext())
    monkeypatch.setattr(checks, "observe_admitted_check_suite", lambda p, pr=None: seen.append((p, pr)))
    monkeypatch.setattr(router, "_resolve_event_identity", AsyncMock(return_value=(None, {"number": 7})))
    monkeypatch.setattr(router, "prepare_route_ordered", AsyncMock(return_value=SimpleNamespace(result="done")))
    monkeypatch.setattr(ingress, "gate_trigger_leg", lambda *_args: True)
    base = {"repository": {"full_name": "o/r"}, "check_suite": {"head_sha": "sha", "conclusion": "success"}}
    for delivery_id, action in (("new", "completed"), ("dup", "completed"), ("held", "completed"), ("other", "requested")):
        await admission._admission_unit("check_suite", {**base, "action": action}, delivery_id, "webhook", object(), [])
        if webhook._BG_DISPATCH:
            await asyncio.gather(*list(webhook._BG_DISPATCH))
    assert len(seen) == 1 and seen[0][1] == {"number": 7}
    await webhook.dispatch_github_webhook_for_replay("check_suite", {**base, "action": "completed"}, delivery_id="held")
    assert len(seen) == 2 and seen[1][1] == {"number": 7}
    if webhook._BG_DISPATCH:
        await asyncio.gather(*list(webhook._BG_DISPATCH))


@pytest.mark.asyncio
async def test_cancelled_suite_uses_payload_pr_without_lookup(monkeypatch):
    from reviewer import gh_helpers

    def forbidden(*_args):
        pytest.fail("unexpected GitHub lookup")

    monkeypatch.setattr(gh_helpers, "get_prs_for_sha", forbidden)
    monkeypatch.setenv("CORSIA_TEST_MODE", "0")
    monkeypatch.setenv(checks.ENABLE_ENV, "1")
    monkeypatch.setattr(router, "emit_product_event", lambda *_args, **_kwargs: None)
    agg = checks.HeadCheckAggregator(lambda *_a, **_kw: None, checks.IntakeSink(), sleep=lambda _: asyncio.Event().wait())
    monkeypatch.setattr(checks, "_default", agg)
    payload = {"action": "completed", "repository": {"full_name": "o/r"},
               "check_suite": {"head_sha": "abc", "head_branch": "main", "conclusion": "cancelled",
                               "pull_requests": [{"number": 7, "head": {"sha": "abc", "ref": "main"},
                                                  "base": {"ref": "dev"}}]}}
    await router.route_event("webhook.github.check_suite_completed", payload)
    assert agg._facts[("o/r", "abc")] == ("main", 7)
    tasks = list(agg._tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
