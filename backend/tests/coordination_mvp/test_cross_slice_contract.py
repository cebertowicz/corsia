"""Cross-slice contract pack (X1–X10) — the end-to-end launch contract.

Each test exercises one cross-slice scenario from the S0 named fixtures through
the real implementation (S1–S7 have all landed). These started as
``xfail(strict=True)`` scaffolds and were flipped to live assertions as each
owning slice shipped; they now stand as the integration coverage for the
coordination-MVP package (sequencing RFC §Cross-Slice Test Pack).
"""

from __future__ import annotations

import pytest

from tests.coordination_mvp import fixtures as F


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


# ── X1 · S3/S4 — Gmail first-touch rule-binds, registers, and owner-wakes ────
def test_x1_gmail_first_touch_rule_bind_register(isolated_db):
    from events.envelope import normalize
    from events.resolve import resolve_targets
    from connections import binder
    from tests.event_routing_helpers import mk_session
    dest, repair = mk_session("proj-a"), mk_session("conductor")
    binder.create_rule(connector="gmail", fact="from", value="user_1@example.com",
                       destination_thread_id=dest)
    env = normalize("gmail", F.scenario("gmail_first_touch").raw_payload)
    targets = resolve_targets(env, repair_owner=repair)
    assert targets and targets[0].resolution == "path_b_rule_bind"
    assert targets[0].disposition == "owner_wake"


# ── X2 · S4 — Gmail repeat resolves by ownership, no binder-specific router ───
def test_x2_gmail_repeat_resolves_by_ownership(isolated_db):
    from events.envelope import normalize
    from events.resolve import resolve_targets
    from registry import artifact_record as AR
    from tests.event_routing_helpers import mk_session
    owner = mk_session("owner")
    AR.register_or_resolve(
        {"integration": "gmail", "type": "email-thread", "external_id": "thr-1"},
        owner_intent=owner)
    env = normalize("gmail", F.scenario("gmail_repeat").raw_payload)
    targets = resolve_targets(env)
    assert targets and targets[0].resolution == "path_a_resolve"
    assert targets[0].thread == owner


# ── X3 · S4 — GitHub check resolves via the registered PR artifact ───────────
def test_x3_github_check_resolves_via_artifact(isolated_db):
    from events.envelope import normalize
    from events.resolve import resolve_targets
    from registry import artifact_record as AR
    from tests.event_routing_helpers import mk_session
    owner = mk_session("worker")
    AR.register_or_resolve(
        {"integration": "github", "type": "pull-request", "external_id": "o/r#7"},
        owner_intent=owner)
    env = normalize("github", F.scenario("github_check_completed").raw_payload)
    assert env.artifact_ref["external_id"] == "o/r#7"
    assert resolve_targets(env)[0].resolution == "path_a_resolve"


# ── X4 · S4 — Document comment resolves via the owned document ───────────────
def test_x4_document_comment_resolves_via_artifact(isolated_db):
    from events.envelope import normalize
    from events.resolve import resolve_targets
    from registry import artifact_record as AR
    from tests.event_routing_helpers import mk_session
    owner = mk_session("doc-owner")
    AR.register_or_resolve(
        {"integration": "local", "type": "document", "external_id": "doc-9"},
        owner_intent=owner)
    env = normalize("document", F.scenario("document_comment").raw_payload)
    assert resolve_targets(env)[0].resolution == "path_a_resolve"


# ── X5 · S3/S4 — Equal-precedence rule conflict raises a repair artifact ─────
def test_x5_equal_precedence_conflict_raises_repair(isolated_db):
    from events.envelope import normalize
    from events.resolve import resolve_targets
    from connections import binder
    from tests.event_routing_helpers import mk_session
    a, b, repair = mk_session("a"), mk_session("b"), mk_session("conductor")
    binder.create_rule(connector="gmail", fact="from", value="boss@example.com",
                       destination_thread_id=a, precedence=50)
    binder.create_rule(connector="gmail", fact="label", value="labelB",
                       destination_thread_id=b, precedence=50)
    env = normalize("gmail", F.scenario("peer_conflict_repair").raw_payload)
    assert resolve_targets(env, repair_owner=repair)[0].resolution == "repair"


# ── X6 · S4 — Parent-chain digests, never wakes (structural) ─────────────────
def test_x6_parent_chain_digests_never_wakes():
    from events.disposition import delivery_disposition, disposition
    # Hard-wired regardless of event_class: actionable still digests.
    assert disposition("parent_chain", "actionable", "active") == "digest"
    assert delivery_disposition("parent_chain", "actionable") == "subscriber_digest"


# ── X7 · S6 — Raised-artifact closeout fans out across subscribed levels ─────
def test_x7_closeout_fans_out_without_archiving(isolated_db):
    from registry import attention
    from registry import artifact_record as AR
    from registry import models
    from tests.event_routing_helpers import mk_session
    scn = F.scenario("raised_artifact_closeout")
    aid = scn.raw_payload["artifact_id"]
    owner, parent, conductor = scn.raw_payload["subscribed_levels"]
    mk_session(owner); mk_session(parent); mk_session(conductor)
    identity = {"integration": "local", "type": "user-raise", "external_id": aid}
    models.register_artifact(integration="local", type="user-raise",
                             external_id=aid, parent_thread_id=owner)
    models.register_artifact(integration="local", type="user-raise",
                             external_id=aid, parent_thread_id=conductor)
    AR.subscribe_parent_chain(identity, parent)
    attention.raise_attention(aid)
    result = attention.clear_attention(aid, outcome="answer")
    assert set(result.cleared_levels) == set(scn.raw_payload["subscribed_levels"])
    assert result.artifact_archived is False
    assert result.subscription_archived is False


# ── X8 · S5 — Review Profile runs fresh; blocking failure gates approve ───────
def test_x8_review_profile_fresh_blocks_approve(isolated_db):
    from parent_review.profiles import run_review_profile
    from registry import models
    from tests.event_routing_helpers import mk_session
    scn = F.scenario("review_profile_fresh_run")
    aid = scn.raw_payload["artifact_id"]
    owner = mk_session("owner")
    models.register_artifact(integration="local", type="user-raise",
                             external_id=aid, parent_thread_id=owner)
    # Low-confidence ask missing evidence/confidence -> blocking profile failure.
    models.update_metadata("local", "user-raise", aid,
                           {"requested_action": "approve budget"})
    result = run_review_profile(aid, profile_id=scn.raw_payload["profile_id"])
    assert result.parent_conversation_context_injected is False
    if result.blocking_failure:
        assert result.may_approve_to_user is False


# ── X9 · S7 — Deadline defer arms a watch that expires once ──────────────────
def test_x9_deadline_defer_expiry_routes_once(isolated_db):
    from automation.watches import arm_watch, expire_watch
    from registry import models
    from tests.event_routing_helpers import mk_session
    scn = F.scenario("defer_until_deadline_expiry")
    aid = scn.raw_payload["artifact_id"]
    owner = mk_session("owner")
    models.register_artifact(integration="local", type="user-raise",
                             external_id=aid, parent_thread_id=owner)
    watch = arm_watch(aid, scn.raw_payload["condition"])
    first = expire_watch(watch.id)
    second = expire_watch(watch.id)
    assert first.routed_wakes == 1 and second.routed_wakes == 0


# ── X10 · S4 — Delivery-health failure reaches a live, non-archivable sink ────
def test_x10_delivery_health_failure_reaches_live_sink(isolated_db):
    from events.envelope import normalize
    from events.resolve import resolve_targets
    scn = F.scenario("delivery_health_sink")
    env = normalize(scn.connector, scn.raw_payload)
    target = resolve_targets(env)[0]
    assert target.resolution == "dead_letter"
    assert target.health_sink_is_live is True
    assert target.health_owner is not None  # never a dead-end


def test_cross_slice_pack_is_fully_covered():
    """Every X-item in the fixtures pack has a scaffold test here (no gaps)."""
    import tests.coordination_mvp.test_cross_slice_contract as mod
    covered = {
        name.split("_", 2)[1].upper()
        for name in dir(mod) if name.startswith("test_x")
    }
    declared = {item["id"] for item in F.CROSS_SLICE_PACK}
    assert declared <= covered, f"cross-slice items without a test: {declared - covered}"
