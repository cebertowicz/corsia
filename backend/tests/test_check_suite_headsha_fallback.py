"""check_suite → PR resolution falls back to head_sha when GitHub delivers an
empty ``pull_requests`` array.

Regression for the #704 failure: a green check_suite with an empty
``pull_requests`` array stranded the PR (owner never woken, reviewer gate
skipped). Both the router's identity resolution and the reviewer-gate route
now share one resolver that falls back to ``/commits/{sha}/pulls``. Connector
normalization and router base-branch checks use the same event-level metadata
path, so empty payload-local PR arrays do not create a second identity source.
"""

from __future__ import annotations

import asyncio

import pytest

from events import identity


def _check_suite_payload(
    *, pull_requests, head_sha="abc123", head_branch="feature/foo", repo="cebertowicz/corsia"
):
    return {
        "repository": {"full_name": repo},
        "check_suite": {
            "id": 12345,
            "head_sha": head_sha,
            "head_branch": head_branch,
            "conclusion": "success",
            "pull_requests": pull_requests,
        },
    }


def _pr(
    number: int,
    *,
    state="open",
    base_ref="dev",
    head_ref="feature/foo",
    head_sha="abc123",
):
    return {
        "number": number,
        "state": state,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "head_sha": head_sha,
    }


class TestResolver:
    def test_populated_array_without_head_metadata_falls_back_to_current_head(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(704)]
        )
        payload = _check_suite_payload(pull_requests=[{"number": 704}])
        assert identity.pr_number_from_check_suite_event(payload) == 704
        assert called == [("cebertowicz/corsia", "abc123")]

    def test_populated_single_pr_array_matches_current_head(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(999)]
        )
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 704, "head": {"ref": "feature/foo", "sha": "abc123"}},
            ],
            head_branch="feature/foo",
        )
        assert identity.pr_number_from_check_suite_event(payload) == 704
        # The lookup MAY run to backfill the missing base_ref; identity stays
        # payload-locked (the API returned #999 above and did not win).

    def test_populated_single_pr_array_rejects_stale_head(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda *a: called.append(a) or [_pr(704, head_sha="newer-sha")],
        )
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 704, "head": {"ref": "feature/foo", "sha": "newer-sha"}},
            ],
            head_branch="feature/foo",
        )
        assert identity.pr_number_from_check_suite_event(payload) is None
        assert called == [("cebertowicz/corsia", "abc123")]

    def test_populated_multi_pr_array_filters_by_current_head(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(999)]
        )
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 700, "head": {"ref": "other", "sha": "abc123"}},
                {"number": 704, "head": {"ref": "feature/foo", "sha": "abc123"}},
            ],
            head_branch="feature/foo",
        )
        assert identity.pr_number_from_check_suite_event(payload) == 704
        # Enrichment lookup allowed; identity stays payload-locked (#704).

    def test_populated_multi_pr_array_without_head_match_is_loud(self):
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 700, "head": {"ref": "other", "sha": "abc123"}},
                {"number": 704, "head": {"ref": "also-other", "sha": "abc123"}},
            ],
            head_branch="feature/foo",
        )
        with pytest.raises(identity.PrResolutionError):
            identity.pr_number_from_check_suite_event(payload)

    def test_empty_array_falls_back_to_head_sha(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda repo, sha: [_pr(704)]
        )
        payload = _check_suite_payload(pull_requests=[])
        assert identity.pr_number_from_check_suite_event(payload) == 704

    def test_empty_array_and_no_pr_for_sha(self, monkeypatch):
        monkeypatch.setattr("reviewer.gh_helpers.get_prs_for_sha", lambda repo, sha: [])
        payload = _check_suite_payload(pull_requests=[])
        assert identity.pr_number_from_check_suite_event(payload) is None

    def test_empty_array_and_fetch_error_is_loud(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda repo, sha: None
        )  # rate-limit / error
        payload = _check_suite_payload(pull_requests=[])
        with pytest.raises(identity.PrResolutionError):
            identity.pr_number_from_check_suite_event(payload)

    def test_empty_array_missing_head_sha_no_call(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(1)]
        )
        payload = _check_suite_payload(pull_requests=[], head_sha="")
        assert identity.pr_number_from_check_suite_event(payload) is None
        assert called == []  # no head_sha → nothing to resolve, no API call

    def test_multiple_prs_filter_by_current_head_branch(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda repo, sha: [
                _pr(700, head_ref="other-branch"),
                _pr(704, head_ref="feature/foo"),
                _pr(705, state="closed", head_ref="feature/foo", head_sha="oldsha"),
            ],
        )
        payload = _check_suite_payload(pull_requests=[], head_branch="feature/foo")
        assert identity.pr_number_from_check_suite_event(payload) == 704

    def test_closed_pr_still_routes_when_current_head_matches(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda repo, sha: [_pr(704, state="closed")],
        )
        payload = _check_suite_payload(pull_requests=[])
        assert identity.pr_number_from_check_suite_event(payload) == 704

    def test_superseded_sha_does_not_route_to_current_pr(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda repo, sha: [_pr(704, head_sha="newer-sha")],
        )
        payload = _check_suite_payload(pull_requests=[])
        assert identity.pr_number_from_check_suite_event(payload) is None

    def test_closed_historical_match_does_not_beat_open_pr(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda repo, sha: [
                _pr(703, state="closed"),
                _pr(704, state="open"),
            ],
        )
        payload = _check_suite_payload(pull_requests=[], head_branch="feature/foo")
        assert identity.pr_number_from_check_suite_event(payload) == 704

    def test_multiple_current_head_branch_matches_are_loud(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda repo, sha: [_pr(704), _pr(705)],
        )
        payload = _check_suite_payload(pull_requests=[], head_branch="feature/foo")
        with pytest.raises(identity.PrResolutionError):
            identity.pr_number_from_check_suite_event(payload)


    def test_partial_metadata_missing_base_enriches_via_head_sha(self, monkeypatch):
        """A payload entry with a matching head but NO base must be enriched —
        router main-failure classification consumes ``base_ref`` (flag
        ccd2d5dcc199d432: partial-metadata shape misclassified main reds)."""
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda *a: called.append(a) or [_pr(704, base_ref="main")],
        )
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 704, "head": {"ref": "feature/foo", "sha": "abc123"}}
            ]
        )
        selected = identity.check_suite_pr_metadata_from_event(payload)
        assert called, "head-SHA enrichment lookup must run"
        assert selected["number"] == 704
        assert selected["base_ref"] == "main"

    def test_partial_metadata_keeps_payload_identity_when_lookup_finds_none(
        self, monkeypatch
    ):
        monkeypatch.setattr("reviewer.gh_helpers.get_prs_for_sha", lambda *a: [])
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 704, "head": {"ref": "feature/foo", "sha": "abc123"}}
            ]
        )
        selected = identity.check_suite_pr_metadata_from_event(payload)
        assert selected["number"] == 704
        assert selected["base_ref"] == ""

    def test_partial_metadata_lookup_failure_degrades_to_payload_identity(
        self, monkeypatch
    ):
        """Enrichment failure must not dead-letter an identity-known event."""
        monkeypatch.setattr("reviewer.gh_helpers.get_prs_for_sha", lambda *a: None)
        payload = _check_suite_payload(
            pull_requests=[
                {"number": 704, "head": {"ref": "feature/foo", "sha": "abc123"}}
            ]
        )
        selected = identity.check_suite_pr_metadata_from_event(payload)
        assert selected["number"] == 704
        assert selected["base_ref"] == ""

    def test_complete_payload_metadata_skips_lookup(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or []
        )
        payload = _check_suite_payload(
            pull_requests=[{
                "number": 704,
                "base": {"ref": "dev"},
                "head": {"ref": "feature/foo", "sha": "abc123"},
            }]
        )
        selected = identity.check_suite_pr_metadata_from_event(payload)
        assert not called, "complete payload metadata must not trigger a GH lookup"
        assert selected["base_ref"] == "dev"


class TestResolveIdentityEndToEnd:
    def test_check_suite_resolves_owner_via_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda repo, sha: [_pr(704)]
        )
        payload = _check_suite_payload(pull_requests=[])
        rid = identity.resolve_identity("webhook.github.check_suite_completed", payload)
        assert rid is not None
        assert rid.external_id == "cebertowicz/corsia#704"
        assert rid.type == identity.PR_TYPE


class TestConnectorNormalization:
    def test_check_suite_artifact_ref_keeps_payload_pr_identity(self, monkeypatch):
        from events.envelope import normalize

        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(999)]
        )
        envelope = normalize(
            "github",
            _check_suite_payload(
                pull_requests=[
                    {"number": 704, "head": {"ref": "feature/foo", "sha": "abc123"}},
                ]
            ),
        )

        assert envelope.artifact_ref is not None
        assert envelope.artifact_ref["external_id"] == "cebertowicz/corsia#704"
        assert envelope.relationships[0]["to_ref"] == "cebertowicz/corsia#704"
        # base_ref enrichment may call the API; the payload PR (#704) keeps
        # identity even though the lookup returned #999.

    def test_check_suite_facts_use_selected_pr_base_branch(self, monkeypatch):
        from events.envelope import normalize

        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(999)]
        )
        payload = _check_suite_payload(
            pull_requests=[
                {
                    "number": 700,
                    "base": {"ref": "main"},
                    "head": {"ref": "other", "sha": "abc123"},
                },
                {
                    "number": 704,
                    "base": {"ref": "dev"},
                    "head": {"ref": "feature/foo", "sha": "abc123"},
                },
            ],
            head_branch="feature/foo",
        )

        envelope = normalize("github", payload)

        assert envelope.artifact_ref is not None
        assert envelope.artifact_ref["external_id"] == "cebertowicz/corsia#704"
        assert envelope.facts["branch"] == "dev"
        assert called == []

    def test_check_suite_artifact_ref_falls_back_without_verifiable_payload_head(
        self, monkeypatch
    ):
        from events.envelope import normalize

        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(704)]
        )
        envelope = normalize("github", _check_suite_payload(pull_requests=[{"number": 704}]))

        assert envelope.artifact_ref is not None
        assert envelope.artifact_ref["external_id"] == "cebertowicz/corsia#704"
        assert envelope.relationships[0]["to_ref"] == "cebertowicz/corsia#704"
        assert called == [("cebertowicz/corsia", "abc123")]

    def test_check_suite_artifact_ref_falls_back_when_payload_prs_empty(self, monkeypatch):
        from events.envelope import normalize

        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: called.append(a) or [_pr(704)]
        )
        envelope = normalize("github", _check_suite_payload(pull_requests=[]))

        assert envelope.artifact_ref is not None
        assert envelope.artifact_ref["external_id"] == "cebertowicz/corsia#704"
        assert envelope.relationships[0]["to_ref"] == "cebertowicz/corsia#704"
        assert called == [("cebertowicz/corsia", "abc123")]

    def test_check_suite_facts_use_fallback_base_branch_when_payload_prs_empty(
        self, monkeypatch
    ):
        from events.envelope import normalize

        called = []
        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda *a: called.append(a)
            or [_pr(704, base_ref="main", head_ref="feature/foo", head_sha="abc123")],
        )

        payload = _check_suite_payload(pull_requests=[], head_branch="feature/foo")
        envelope = normalize("github", payload)

        assert envelope.artifact_ref is not None
        assert envelope.artifact_ref["external_id"] == "cebertowicz/corsia#704"
        assert envelope.facts["branch"] == "main"


class TestRouterCancellation:
    def test_cancelled_check_suite_drops_before_identity_resolution(self, monkeypatch):
        from events import router

        def fail_if_called(*args, **kwargs):
            raise AssertionError("cancelled check_suite should not resolve identity")

        monkeypatch.setattr(router, "resolve_identity", fail_if_called)
        payload = _check_suite_payload(pull_requests=[])
        payload["check_suite"]["conclusion"] = "cancelled"

        result = asyncio.run(
            router.route_event("webhook.github.check_suite_completed", payload)
        )

        assert (result.outcome, result.reason) == (
            router.RouteOutcome.SUPPRESSED, "rule_set:github@1:cancelled")


class TestReviewerRouteResolutionFailure:
    def test_resolution_failure_sets_reviewer_status_error(self, monkeypatch):
        from events import identity as identity_mod
        from events import reviewer_route

        status_calls = []

        def fail_resolution(payload):
            raise identity_mod.PrResolutionError("lookup failed")

        monkeypatch.setattr(identity_mod, "pr_number_from_check_suite_event", fail_resolution)
        monkeypatch.setattr(
            "reviewer.reviewer_status.set_error",
            lambda repo, sha, description: status_calls.append((repo, sha, description)),
        )
        payload = _check_suite_payload(pull_requests=[])

        reviewer_route.check_gate_on_trigger("webhook.github.check_suite_completed", payload)

        assert status_calls == [
            (
                "cebertowicz/corsia",
                "abc123",
                "qa/llm-reviewer: PR resolution error — lookup failed",
            )
        ]


class TestNoOwnerMainFailureRouting:
    def test_empty_array_main_failure_dead_letters_after_head_sha_fallback(
        self, monkeypatch
    ):
        from events import router

        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha",
            lambda *a: [
                _pr(704, base_ref="main", head_ref="feature/foo", head_sha="abc123")
            ],
        )
        monkeypatch.setattr(
            router,
            "resolve_targets",
            lambda envelope: [
                type(
                    "Target",
                    (),
                    {
                        "thread": None,
                        "relationship": None,
                        "reason": "no_owner",
                        "routing_state": {
                            "identity": {"id": "artifact-1"},
                            "owner": None,
                            "subscribers": [],
                            "triage": None,
                        },
                    },
                )()
            ],
        )
        dead_letters = []
        monkeypatch.setattr(
            router.dead_letter,
            "park",
            lambda internal_event, payload, reason, external_id: dead_letters.append(
                (reason, external_id)
            )
            or "dl-1",
        )
        monkeypatch.setattr(router, "emit_product_event", lambda *a, **k: None)

        payload = _check_suite_payload(pull_requests=[])
        payload["check_suite"]["conclusion"] = "failure"

        result = asyncio.run(
            router.route_event("webhook.github.check_suite_completed", payload)
        )

        assert result.outcome == router.RouteOutcome.DEAD_LETTERED
        assert result.reason == "no_owner"
        assert result.external_id == "cebertowicz/corsia#704"
        assert dead_letters == [("no_owner", "cebertowicz/corsia#704")]

    def test_main_suite_with_unresolved_base_dead_letters_not_parks(
        self, monkeypatch
    ):
        """Enrichment can miss (base unknown), but a suite whose own
        head_branch is main is a push-to-main failure — it must dead-letter,
        never park as noise. Feature-branch unknowns still park (see the
        conductor noise-filter suite)."""
        from events import router

        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: [_pr(999)]
        )
        monkeypatch.setattr(
            router,
            "resolve_targets",
            lambda envelope: [
                type(
                    "Target",
                    (),
                    {
                        "thread": None,
                        "relationship": None,
                        "reason": "no_owner",
                        "routing_state": {
                            "identity": {"id": "artifact-1"},
                            "owner": None,
                            "subscribers": [],
                            "triage": None,
                        },
                    },
                )()
            ],
        )
        dead_letters = []
        monkeypatch.setattr(
            router.dead_letter,
            "park",
            lambda internal_event, payload, reason, external_id: dead_letters.append(
                (reason, external_id)
            )
            or "dl-1",
        )
        monkeypatch.setattr(router, "emit_product_event", lambda *a, **k: None)

        payload = _check_suite_payload(
            pull_requests=[], head_sha=None, head_branch="main"
        )
        payload["check_suite"]["conclusion"] = "failure"

        result = asyncio.run(
            router.route_event("webhook.github.check_suite_completed", payload)
        )

        assert result.outcome == router.RouteOutcome.DEAD_LETTERED
        assert result.reason == "no_owner"
        assert len(dead_letters) == 1 and dead_letters[0][0] == "no_owner"

    def test_pr_bound_suite_with_source_branch_main_parks_as_noise(
        self, monkeypatch
    ):
        """head_branch on a PR-BOUND suite is the PR's SOURCE branch — a fork
        branch literally named "main" must not dead-letter as a main failure
        when base enrichment misses (reviewer flag ea5d7ec1f3011245)."""
        from events import router

        monkeypatch.setattr(
            "reviewer.gh_helpers.get_prs_for_sha", lambda *a: [_pr(999)]
        )
        monkeypatch.setattr(
            router,
            "resolve_targets",
            lambda envelope: [
                type(
                    "Target",
                    (),
                    {
                        "thread": None,
                        "relationship": None,
                        "reason": "no_owner",
                        "routing_state": {
                            "identity": {"id": "artifact-1"},
                            "owner": None,
                            "subscribers": [],
                            "triage": None,
                        },
                    },
                )()
            ],
        )
        parked = []
        monkeypatch.setattr(
            router.dead_letter,
            "park",
            lambda internal_event, payload, reason, external_id: parked.append(
                reason
            )
            or "dl-1",
        )
        monkeypatch.setattr(router, "emit_product_event", lambda *a, **k: None)

        payload = _check_suite_payload(
            pull_requests=[
                {"number": 704, "head": {"ref": "main", "sha": "abc123"}}
            ],
            head_branch="main",
        )
        payload["check_suite"]["conclusion"] = "failure"

        result = asyncio.run(
            router.route_event("webhook.github.check_suite_completed", payload)
        )

        assert result.outcome == router.RouteOutcome.PARKED
        assert parked == ["check_suite_noise_parked"]

    def test_no_owner_main_failure_reuses_resolved_check_suite_metadata(
        self, monkeypatch
    ):
        from events import router

        calls = []

        def resolve_prs(repo, sha):
            calls.append((repo, sha))
            if len(calls) > 1:
                raise AssertionError("check_suite PR metadata resolved more than once")
            return [_pr(704, base_ref="main", head_ref="feature/foo", head_sha="abc123")]

        monkeypatch.setattr("reviewer.gh_helpers.get_prs_for_sha", resolve_prs)
        monkeypatch.setattr(
            router,
            "resolve_targets",
            lambda envelope: [
                type(
                    "Target",
                    (),
                    {
                        "thread": None,
                        "relationship": None,
                        "reason": "no_owner",
                        "routing_state": {
                            "identity": {"id": "artifact-1"},
                            "owner": None,
                            "subscribers": [],
                            "triage": None,
                        },
                    },
                )()
            ],
        )
        monkeypatch.setattr(router.dead_letter, "park", lambda *a, **k: "dl-1")
        monkeypatch.setattr(router, "emit_product_event", lambda *a, **k: None)

        payload = _check_suite_payload(pull_requests=[])
        payload["check_suite"]["conclusion"] = "failure"

        result = asyncio.run(
            router.route_event("webhook.github.check_suite_completed", payload)
        )

        assert result.outcome == router.RouteOutcome.DEAD_LETTERED
        assert result.reason == "no_owner"
        assert calls == [("cebertowicz/corsia", "abc123")]
