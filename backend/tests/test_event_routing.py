from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from infra import corsia_agent

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.turn_core import TurnIdentity

from shared_send_fixtures import _install_fake_native_context_agent
from tests.event_routing_helpers import mk_session as _mk_session
from tests.event_routing_helpers import pr_payload as _pr_payload
from tests.event_routing_helpers import register_pr as _register_pr
from tests.event_routing_helpers import route as _route


async def _accept_cc_bound_turn(*_args, **kwargs):
    await kwargs["persist_input_once"]()
    kwargs["mark_accepted"]()
    await kwargs["settle_delivery"]()
    return kwargs.get("preadmitted_turn") or TurnIdentity(kwargs["turn_id"], 1)


def _accept_codex_active(owner_turn_id: str):
    async def _accept(*_args, **kwargs):
        successor_turn_id = kwargs["successor_turn_id"]
        preadmitted_turn = kwargs.get("preadmitted_turn")
        accepted_turn_id = (
            preadmitted_turn.turn_id
            if preadmitted_turn is not None
            else owner_turn_id
        )
        await kwargs["bind_input_turn"](
            accepted_turn_id,
            successor_turn_id if accepted_turn_id != successor_turn_id else None,
        )
        kwargs["mark_accepted"]()
        await kwargs["settle_delivery"]()
        return preadmitted_turn or TurnIdentity(owner_turn_id, 1)

    return _accept


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


@pytest.fixture(autouse=True)
def _native_context_agent(monkeypatch):
    _install_fake_native_context_agent(monkeypatch)


class TestIdentity:
    def test_pr_event_resolves_to_pr_identity(self):
        from events.identity import resolve_identity
        rid = resolve_identity(
            "webhook.github.pr_opened",
            {"repository": {"full_name": "o/r"}, "pull_request": {"number": 5}},
        )
        assert rid == ("github", "pull-request", "o/r#5")

    def test_ready_for_review_resolves_to_pr_identity(self):
        from events.identity import resolve_identity

        rid = resolve_identity(
            "webhook.github.pr_ready_for_review",
            {
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 17, "head": {"sha": "abc123"}},
            },
        )
        assert rid == ("github", "pull-request", "o/r#17")

    def test_issue_comment_on_pr_resolves_to_pr_identity(self):
        from events.identity import resolve_identity
        rid = resolve_identity(
            "webhook.github.pr_comment",
            {"repository": {"full_name": "o/r"},
             "issue": {"number": 9, "pull_request": {"url": "..."}}},
        )
        assert rid.external_id == "o/r#9"

    def test_check_suite_completed_resolves_to_parent_pr(self):
        from events.identity import resolve_identity
        rid = resolve_identity(
            "webhook.github.check_suite_completed",
            {"repository": {"full_name": "o/r"},
             "check_suite": {"pull_requests": [{"number": 12}]}},
        )
        assert rid.external_id == "o/r#12"

    def test_check_run_event_is_not_identity_resolvable(self):
        from events.identity import resolve_identity
        for event in ["webhook.github.check_run_completed",
                      "webhook.github.check_run_created"]:
            rid = resolve_identity(
                event,
                {"repository": {"full_name": "o/r"},
                 "check_run": {"pull_requests": [{"number": 12}]}},
            )
            assert rid is None, f"{event} should not resolve to an identity"

    def test_no_repo_is_unresolvable(self):
        from events.identity import resolve_identity
        assert resolve_identity("webhook.github.pr_opened", {}) is None

    def test_pr_type_constant_shared_with_registration_boundary(self):
        """#674 — routing and registration must never re-drift independently."""
        from events import identity
        from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
        assert identity.PR_TYPE is PR_TYPE
        assert identity.GITHUB_INTEGRATION is GITHUB_INTEGRATION

class TestRouting:
    @pytest.mark.asyncio
    async def test_routes_to_registered_owning_thread(self, isolated_db):
        owner = _mk_session("own-1")
        _register_pr("o/r", 42, owner)

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route("webhook.github.pr_opened", _pr_payload("o/r", 42))

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == owner
        assert mock_deliver.call_args.args[0] == owner

    @pytest.mark.asyncio
    async def test_check_suite_delivery_identity_and_provenance_reach_owner(
        self, isolated_db
    ):
        owner = _mk_session("own-check-delivery")
        _register_pr("o/r", 1140, owner)
        payload = {
            "action": "completed",
            "repository": {"full_name": "o/r"},
            "check_suite": {
                "id": 4100,
                "conclusion": "success",
                "head_sha": "head-1140",
                "updated_at": "2026-08-16T10:00:00Z",
                "pull_requests": [
                    {
                        "number": 1140,
                        "base": {"ref": "dev"},
                        "head": {"sha": "head-1140"},
                    }
                ],
            },
            "sender": {"login": "alice", "type": "User"},
        }

        from events.router import RouteOutcome, route_event

        with patch(
            "events.router.delivery.deliver_to_thread", return_value=True
        ) as mock_deliver:
            result = await route_event(
                "webhook.github.check_suite_completed",
                payload,
                delivery_id="github-delivery-1140",
            )

        assert result.outcome == RouteOutcome.ACCEPTED
        assert result.thread_id == owner
        call = mock_deliver.call_args
        assert call.kwargs["delivery_id"] == "github-delivery-1140"
        assert call.args[3] == {
            "repo": "o/r",
            "external_id": "o/r#1140",
            "check_suite_id": 4100,
            "head_sha": "head-1140",
            "check_suite_updated_at": "2026-08-16T10:00:00Z",
        }

    @pytest.mark.asyncio
    async def test_agent_registration_with_drifted_type_still_routes(self, isolated_db):
        """#674 regression: an agent registering with type='pull_request' (the
        observed drift) must still canonicalize to the routing-resolvable
        identity, end to end through the register tool + routing lookup."""
        from tools.artifacts import ArtifactRegisterTool

        owner = _mk_session("own-674")
        result = await ArtifactRegisterTool().execute(
            integration="github",
            type="pull_request",
            external_id="cebertowicz/corsia#674",
            title="PR #674",
            _thread_id=owner,
        )
        payload = json.loads(result)
        assert payload["artifact"]["type"] == "pull-request"

        from registry.models import routing_registrations
        routing = routing_registrations("github", "pull-request", "cebertowicz/corsia#674")
        assert routing["owner"]["parent_thread_id"] == owner

        with patch("events.router.delivery.deliver_to_thread",
                   return_value=True) as mock_deliver:
            res = await _route(
                "webhook.github.pr_opened",
                _pr_payload("cebertowicz/corsia", 674),
            )
        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == owner
        assert mock_deliver.call_args.args[0] == owner

    @pytest.mark.asyncio
    async def test_bare_number_external_id_rejected_loud(self, isolated_db):
        from tools.artifacts import ArtifactRegisterTool

        owner = _mk_session("own-bare-num")
        result = await ArtifactRegisterTool().execute(
            integration="github",
            type="pull_request",
            external_id="679",
            title="PR #679",
            _thread_id=owner,
        )
        assert result.startswith("Error:")
        assert "679" in result
        assert "<owner>/<repo>#<N>" in result

    @pytest.mark.asyncio
    async def test_pr_merged_records_artifact_projection_fact(self, isolated_db):
        owner = _mk_session("own-pr-merged")
        _register_pr("o/r", 88, owner)
        payload = _pr_payload("o/r", 88, action="closed")
        payload["pull_request"]["merged"] = True

        with patch("events.router.delivery.deliver_to_thread", return_value=True):
            res = await _route("webhook.github.pr_merged", payload)

        from events.router import RouteOutcome
        from registry import models, view
        assert res.outcome == RouteOutcome.ACCEPTED
        row = models.resolve("github", "pull-request", "o/r#88")
        assert row is not None
        import json
        assert json.loads(row["metadata"])["pr_state"] == "merged"

        # The merge is the PR's own terminal event, so the row it occupied
        # leaves the pane in the same write that records the state.
        pane = view.pane_for_thread(owner)
        assert not [n for n in pane["nodes"] if n["id"] == row["id"]]

    @pytest.mark.asyncio
    async def test_pr_closed_without_merge_retires_artifact_before_delivery(self, isolated_db):
        owner = _mk_session("own-pr-closed")
        _register_pr("o/r", 91, owner)
        payload = _pr_payload("o/r", 91, action="closed")
        payload["pull_request"]["merged"] = False

        with patch("events.router.delivery.deliver_to_thread", return_value=True):
            res = await _route("webhook.github.pr_closed", payload)

        from events.router import RouteOutcome
        from registry import models, view
        assert res.outcome == RouteOutcome.ACCEPTED
        row = models.resolve("github", "pull-request", "o/r#91")
        assert row is not None
        assert json.loads(row["metadata"])["pr_state"] == "closed"
        pane = view.pane_for_thread(owner)
        assert not [n for n in pane["nodes"] if n["id"] == row["id"]]

    @pytest.mark.asyncio
    async def test_pr_closed_source_fact_requires_unmerged_payload(self, isolated_db):
        owner = _mk_session("own-pr-closed-mismatch")
        _register_pr("o/r", 92, owner)
        payload = _pr_payload("o/r", 92, action="closed")
        payload["pull_request"]["merged"] = True

        with patch("events.router.delivery.deliver_to_thread", return_value=True):
            await _route("webhook.github.pr_closed", payload)

        from registry import models
        row = models.resolve("github", "pull-request", "o/r#92")
        assert row is not None
        assert row["metadata"] is None

    @pytest.mark.asyncio
    async def test_pr_merged_source_fact_requires_merged_payload(self, isolated_db):
        owner = _mk_session("own-pr-not-merged")
        _register_pr("o/r", 89, owner)
        payload = _pr_payload("o/r", 89, action="closed")
        payload["pull_request"]["merged"] = False

        with patch("events.router.delivery.deliver_to_thread", return_value=True):
            await _route("webhook.github.pr_merged", payload)

        from registry import models
        row = models.resolve("github", "pull-request", "o/r#89")
        assert row is not None
        assert row["metadata"] is None

    @pytest.mark.asyncio
    async def test_pr_merged_source_fact_survives_self_suppression(self, isolated_db):
        owner = _mk_session("own-pr-merged-bot")
        _register_pr("o/r", 90, owner)
        payload = _pr_payload(
            "o/r", 90, action="closed", author="corsia[bot]", author_type="Bot"
        )
        payload["pull_request"]["merged"] = True
        payload["pull_request"]["user"] = {"login": "corsia[bot]", "type": "Bot"}  # suppression reads the content author
        nudges: list[dict] = []

        with patch(
            "plans.notifications.state.broadcast_to_user",
            lambda _user_id, message: nudges.append(message),
        ), patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.pr_merged", payload)

        from events.router import RouteOutcome
        from registry import models
        assert res.outcome == RouteOutcome.SUPPRESSED
        mock_deliver.assert_not_called()
        row = models.resolve("github", "pull-request", "o/r#90")
        assert row is not None
        import json
        assert json.loads(row["metadata"])["pr_state"] == "merged"
        assert len(nudges) == 1
        assert nudges[0]["type"] == "artifacts_changed"
        assert nudges[0]["artifact_id"] == row["id"]
        assert nudges[0]["affected_thread_ids"] == [owner]

    @pytest.mark.asyncio
    async def test_routes_live_codex_owner_as_delivered(self, isolated_db):
        from subagents import codex_runtime
        owner = _mk_session(
            "codex-own-1", spawned_by="parent", model="codex/gpt-5-codex",
        )
        _register_pr("o/r", 43, owner)

        feed = AsyncMock(side_effect=_accept_codex_active("turn-route"))
        with patch("subagents.codex_runtime.ensure_connection_available",
                   lambda db=None: None), \
             patch.dict(
                 codex_runtime._CODEX_THREADS,
                 {"codex-own-1": "provider-thread-codex-own-1"},
             ), \
             patch("subagents.codex_runtime.feed", feed):
            res = await _route("webhook.github.pr_opened", _pr_payload("o/r", 43))

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.ACCEPTED
        assert res.thread_id == owner
        feed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_owner_does_not_fall_back_to_workspace_conductor(self, isolated_db):
        conductor_thread = _mk_session("cond-1")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.pr_opened", _pr_payload("o/r", 77))

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.thread_id is None
        assert res.reason == "no_owner"
        mock_deliver.assert_not_called()

    @pytest.mark.asyncio
    async def test_unroutable_dead_letters_never_misdelivers(self, isolated_db):
        _mk_session("bystander")

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.pr_opened", _pr_payload("o/r", 99))

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        mock_deliver.assert_not_called()

        from events import dead_letter
        parked = dead_letter.list_parked()
        assert len(parked) == 1
        assert parked[0]["external_id"] == "o/r#99"
        assert parked[0]["internal_event"] == "webhook.github.pr_opened"

    @pytest.mark.asyncio
    async def test_archived_owner_dead_letters_without_workspace_guess(self, isolated_db):
        owner = _mk_session("own-arch")
        _register_pr("o/r", 5, owner)
        conductor_thread = _mk_session("cond-2")
        from infra import conductor
        corsia_agent.set_corsia_agent(conductor_thread)

        from infra.database import get_db
        db = get_db()
        db.execute(
            "UPDATE threads SET lifecycle_state = 'archived', "
            "archived_reason = 'user' WHERE id = ?",
            (owner,),
        )
        db.commit()
        db.close()

        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route("webhook.github.pr_opened", _pr_payload("o/r", 5))

        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.DEAD_LETTERED
        assert res.thread_id is None
        assert res.reason == "owner_inactive"
        mock_deliver.assert_not_called()
        assert conductor_thread != owner

class TestGuard:
    def test_bot_account_is_suppressed(self):
        from events.guard import is_bot_or_self_authored
        assert is_bot_or_self_authored(
            {"sender": {"login": "corsia-app", "type": "Bot"}})

    def test_bracket_bot_login_is_suppressed(self):
        from events.guard import is_bot_or_self_authored
        assert is_bot_or_self_authored(
            {"comment": {"user": {"login": "corsia[bot]", "type": "User"}}})

    def test_human_is_not_suppressed(self):
        from events.guard import is_bot_or_self_authored
        assert not is_bot_or_self_authored(
            {"sender": {"login": "alice", "type": "User"}})

    def test_configured_self_author_suppressed(self, isolated_db):
        from infra.settings import set_setting
        set_setting("event_routing", {"self_authors": ["my-automation"]})
        from events.guard import is_bot_or_self_authored
        assert is_bot_or_self_authored(
            {"comment": {"user": {"login": "my-automation", "type": "User"}}})

    @pytest.mark.asyncio
    async def test_router_suppresses_bot_event(self, isolated_db):
        with patch("events.router.delivery.deliver_to_thread") as mock_deliver:
            res = await _route(
                "webhook.github.pr_comment",
                {"repository": {"full_name": "o/r"},
                 "issue": {"number": 3, "pull_request": {"url": "x"}},
                 "comment": {"user": {"login": "x[bot]", "type": "Bot"},
                             "body": "looks good"}},
            )
        from events.router import RouteOutcome
        assert res.outcome == RouteOutcome.SUPPRESSED
        mock_deliver.assert_not_called()

class TestReviewerRehome:
    def test_reviewer_does_not_fire_on_push(self):
        from events import reviewer_route
        payload = _pr_payload("o/r", 100, author="alice", author_type="User")
        with patch("reviewer.on_pr_review.on_pr_review") as mock_review:
            reviewer_route.run_review("webhook.github.pr_opened", payload)
        mock_review.assert_not_called()

    def test_reviewer_suppressed_for_bot_author(self):
        from events import reviewer_route
        payload = _pr_payload("o/r", 101, author="ci[bot]", author_type="Bot")
        with patch("reviewer.on_pr_review.on_pr_review") as mock_review:
            reviewer_route.run_review("webhook.github.pr_synchronize", payload)
        mock_review.assert_not_called()

    def test_non_reviewer_event_does_not_review(self):
        from events import reviewer_route
        with patch("reviewer.on_pr_review.on_pr_review") as mock_review:
            reviewer_route.run_review("webhook.github.pr_merged",
                                      _pr_payload("o/r", 102, action="closed"))
        mock_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingress_dead_letter_leaves_reviewer_independent(self, isolated_db):
        from events import ingress
        with patch("events.ingress.run_review") as mock_review:
            res = await ingress.handle_event(
                "webhook.github.pr_opened", _pr_payload("o/r", 201), "dlv-2")
        assert res["route_outcome"] == "dead_lettered"
        assert res["reviewer"] == "armed"
        mock_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingress_runs_both_legs_on_injected_pr_opened(self, isolated_db):
        owner = _mk_session("own-ing")
        _register_pr("o/r", 200, owner)

        from events import ingress
        from events.router import RouteOutcome
        with patch("events.ingress.route_event") as mock_route, \
             patch("events.ingress.run_review") as mock_review:
            mock_route.return_value = MagicMock(
                outcome=RouteOutcome.ACCEPTED, thread_id=owner)
            res = await ingress.handle_event(
                "webhook.github.pr_opened", _pr_payload("o/r", 200), "dlv-1")

        assert res["route_outcome"] == RouteOutcome.ACCEPTED.value == "accepted"
        assert res["thread_id"] == owner
        assert res["reviewer"] == "armed"
        mock_review.assert_called_once()
        mock_route.assert_awaited_once_with(
            "webhook.github.pr_opened",
            _pr_payload("o/r", 200),
            delivery_id="dlv-1",
        )

class TestSystemMessageLLMExclusion:

    def test_activity_receipt_excluded_from_llm_context(self):
        from core.message_history import db_to_llm_history
        rows = [
            {"role": "user", "content": "hi", "metadata": None},
            {"role": "system", "content": "o/r#1 opened",
             "metadata": '{"type": "activity_receipt", "event_type": "webhook.github.pr_opened"}'},
            {"role": "assistant", "content": "On it", "metadata": None},
        ]
        out = db_to_llm_history(rows)
        roles = [m["role"] for m in out]
        assert "system" not in roles
        assert roles == ["user", "assistant"]

    def test_wake_event_replays_as_the_turn_input(self):
        from core.message_history import db_to_llm_history

        content = "[EVENT: system.restart.resume]\nInstance restarted"
        rows = [
            {
                "role": "system",
                "content": content,
                "metadata": json.dumps({
                    "type": "context_event",
                    "event_type": "system.restart.resume",
                    "event_disposition": "wake",
                }),
            },
            {"role": "assistant", "content": "Resumed", "metadata": None},
        ]

        assert db_to_llm_history(rows) == [
            {"role": "user", "content": content},
            {"role": "assistant", "content": "Resumed"},
        ]

    def test_subagent_ref_excluded_from_llm_context(self):
        from core.message_history import db_to_llm_history
        rows = [
            {"role": "user", "content": "spawn a worker", "metadata": None},
            {"role": "system", "content": "Spawned: PR Reviewer",
             "metadata": '{"type": "subagent_ref", "child_thread_id": "abc1", "provider": "delegation"}'},
            {"role": "assistant", "content": "Spawned.", "metadata": None},
        ]
        out = db_to_llm_history(rows)
        roles = [m["role"] for m in out]
        assert "system" not in roles
        assert roles == ["user", "assistant"]


class TestGithubEventClassDerivation:
    """#595 nit 2: event_class is derived from the github manifest, not hardcoded —
    so a future disposition consumer digests informational events (pr.merged)."""

    def test_pr_merged_is_informational(self):
        from events.router import _github_event_class
        merged = {"action": "closed", "repository": {"full_name": "o/r"},
                  "pull_request": {"number": 1, "merged": True}}
        assert _github_event_class(merged) == "informational"

    def test_pr_closed_without_merge_is_informational(self):
        from events.connectors.github import MANIFEST
        from events.router import _github_event_class

        closed = {"action": "closed", "repository": {"full_name": "o/r"},
                  "pull_request": {"number": 1, "merged": False}}
        assert MANIFEST.classify(closed) == "pr.closed"
        assert _github_event_class(closed) == "informational"

    def test_pr_opened_and_check_are_actionable(self):
        from events.router import _github_event_class
        opened = {"action": "opened", "repository": {"full_name": "o/r"},
                  "pull_request": {"number": 1}}
        check = {"action": "completed", "repository": {"full_name": "o/r"},
                 "check_suite": {"id": 9, "conclusion": "failure",
                                 "pull_requests": [{"number": 1}]}}
        assert _github_event_class(opened) == "actionable"
        assert _github_event_class(check) == "actionable"

    def test_ready_for_review_routes_to_gate_without_waking_owner(self):
        from events.connectors.github import MANIFEST
        from events.disposition import disposition
        from events.reviewer_route import GATE_TRIGGER_EVENTS

        ready = {
            "action": "ready_for_review",
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": 1, "head": {"sha": "abc123"}},
        }
        event_type = MANIFEST.classify(ready)
        assert event_type == "pr.ready_for_review"
        assert disposition("owner", MANIFEST.decl(event_type).event_class) == "digest"
        assert "webhook.github.pr_ready_for_review" in GATE_TRIGGER_EVENTS
