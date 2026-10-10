"""S2 — Artifacts register_or_resolve + subscriber kinds.

Proves: one record shape for first-register and resolve; subscriber kinds
(owner/subscriber/parent_chain) + lifecycle; parent_chain can never become owner;
and artifact archive vs subscription archive are distinct operations.
"""

from __future__ import annotations

import pytest

from registry import artifact_record as AR
from registry import models
from registry.identity_bridge import ParentChainOwnershipError
from registry.ownership import rehome_artifact
from tests.event_routing_helpers import mk_session

IDENTITY = {"integration": "gmail", "type": "email-thread", "external_id": "thr-1"}


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _record(identity=IDENTITY):
    return AR.register_or_resolve(identity)


class TestOneRecordShape:
    """register_or_resolve returns the same shape for first-register and resolve."""

    def test_first_register_then_resolve_same_shape(self, isolated_db):
        owner = mk_session("owner-thr")
        first = AR.register_or_resolve(IDENTITY, owner_intent=owner)
        resolved = AR.register_or_resolve(IDENTITY)
        assert first == resolved
        assert first.owner_thread == owner
        assert first.lifecycle == "active"
        # owner appears in subscribers tagged owner (RFC ArtifactRecord shape).
        assert AR.Subscriber(owner, AR.OWNER_KIND) in first.subscribers
        assert first.identity["external_id"] == "thr-1"

    def test_idempotent_register(self, isolated_db):
        owner = mk_session("owner-thr")
        a = AR.register_or_resolve(IDENTITY, owner_intent=owner)
        b = AR.register_or_resolve(IDENTITY, owner_intent=owner)
        assert a == b and a.owner_thread == owner

    def test_unregistered_resolve_is_single_shaped(self, isolated_db):
        rec = AR.register_or_resolve(IDENTITY)
        assert rec.lifecycle == "unregistered"
        assert rec.owner_thread is None and rec.subscribers == ()


class TestSubscriberKinds:
    """Kinds project from relationship_state; parent_chain is its own kind."""

    def test_parent_chain_subscriber_kind(self, isolated_db):
        owner = mk_session("owner-thr")
        parent = mk_session("parent-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        rec = AR.subscribe_parent_chain(IDENTITY, parent)
        assert rec.kind_of(parent) == AR.PARENT_CHAIN_KIND
        assert rec.kind_of(owner) == AR.OWNER_KIND

    def test_plain_subscriber_kind(self, isolated_db):
        owner = mk_session("owner-thr")
        sub = mk_session("sub-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        # A second register on an owned identity becomes a passive subscriber.
        models.register_artifact(integration="gmail", type="email-thread",
                                 external_id="thr-1", parent_thread_id=sub)
        rec = AR.register_or_resolve(IDENTITY)
        assert rec.kind_of(sub) == AR.SUBSCRIBER_KIND
        assert rec.owner_thread == owner  # unchanged by the second register


class TestParentChainNeverOwner:
    """The S2 invariant: a parent_chain subscriber can never become owner."""

    def test_register_or_resolve_never_promotes_parent_chain(self, isolated_db):
        owner = mk_session("owner-thr")
        parent = mk_session("parent-thr")
        # Register to a real owner, make `parent` a parent_chain subscriber, then
        # release ownership so the identity is ownerless-but-registered.
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        AR.subscribe_parent_chain(IDENTITY, parent)
        from registry.ownership import release_artifact
        release_artifact(integration="gmail", type="email-thread",
                         external_id="thr-1", actor_thread_id=owner,
                         authority="test", reason="r", source_path="t")
        assert AR.register_or_resolve(IDENTITY).owner_thread is None
        # owner_intent must NOT re-own a released identity (re-home is explicit),
        # and certainly never promotes the parent_chain thread to owner.
        rec = AR.register_or_resolve(IDENTITY, owner_intent=parent)
        assert rec.owner_thread is None
        assert rec.kind_of(parent) == AR.PARENT_CHAIN_KIND

    def test_rehome_to_parent_chain_rejected(self, isolated_db):
        owner = mk_session("owner-thr")
        parent = mk_session("parent-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        AR.subscribe_parent_chain(IDENTITY, parent)
        with pytest.raises(ParentChainOwnershipError):
            rehome_artifact(integration="gmail", type="email-thread",
                            external_id="thr-1", new_owner_thread_id=parent,
                            actor_thread_id=owner, authority="test", reason="r",
                            source_path="t")

    def test_owner_cannot_also_be_parent_chain(self, isolated_db):
        owner = mk_session("owner-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        with pytest.raises(ParentChainOwnershipError):
            AR.subscribe_parent_chain(IDENTITY, owner)


class TestArchiveDistinctFromSubscriptionArchive:
    """Artifact archive (everyone) is a different op from one thread's unsubscribe."""

    def test_remove_subscription_leaves_artifact_active(self, isolated_db):
        owner = mk_session("owner-thr")
        parent = mk_session("parent-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        AR.subscribe_parent_chain(IDENTITY, parent)
        rec = AR.remove_subscription(IDENTITY, parent)
        assert rec.kind_of(parent) is None          # subscription gone
        assert rec.lifecycle == "active"            # artifact untouched
        assert rec.owner_thread == owner            # owner untouched

    def test_artifact_archive_changes_lifecycle_for_everyone(self, isolated_db):
        owner = mk_session("owner-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        artifact_id = AR.register_or_resolve(IDENTITY).identity["id"]
        models.retire(artifact_id)                  # artifact archive
        rec = AR.register_or_resolve(IDENTITY)
        assert rec.lifecycle == "archived"

    def test_owner_subscription_not_removable_here(self, isolated_db):
        owner = mk_session("owner-thr")
        AR.register_or_resolve(IDENTITY, owner_intent=owner)
        with pytest.raises(ParentChainOwnershipError):
            AR.remove_subscription(IDENTITY, owner)
