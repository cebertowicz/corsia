"""S6b — user-attention HTTP surface (API behavior for the launch UI).

Exercises the endpoints over the real app: the Requires-user list, review-profile
result, parent disposition (incl. the blocking-failure 409 gate + authorized
override), clearing, rollup (pull-only), and conductor surface. Seeds artifacts
directly into the shared app DB, then drives them through HTTP.
"""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from infra import corsia_agent

import sys
import os
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytestmark = pytest.mark.integration

_TOKEN = OWNER_TOKEN
_GOOD = {"requested_action": "approve budget", "evidence": "Q3", "confidence": "high",
         "urgency": "high", "attention_state": "raised"}
_BAD = {"requested_action": "approve budget", "attention_state": "raised"}


def _seed(external_id: str, snapshot: dict, *, owner: str = "owner-thr",
          parent: str | None = None) -> None:
    """Register a raised user-raise (+ optional parent_chain level) in the app DB."""
    from datetime import datetime, timezone
    from infra.database import get_db
    from registry import models
    from registry import artifact_record as AR

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    for thread in {owner, parent} - {None}:
        db.execute(
            "INSERT OR IGNORE INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (thread, "steve", "T", now, now))
    db.commit()
    models.register_artifact(integration="local", type="user-raise",
                             external_id=external_id, parent_thread_id=owner, db=db)
    models.update_metadata("local", "user-raise", external_id, snapshot, db=db)
    if parent:
        AR.subscribe_parent_chain(
            {"integration": "local", "type": "user-raise", "external_id": external_id},
            parent, db=db)
    db.commit()


def _eid() -> str:
    return f"ur-{uuid.uuid4().hex[:8]}"


def _H(token: str) -> dict:
    """Auth header for GET endpoints (token rides a Bearer header, not the URL)."""
    return {"Authorization": f"Bearer {token}"}


class TestRaisedList:
    def test_raised_list_includes_seeded_item(self, integration_client):
        aid = _eid()
        _seed(aid, _GOOD)
        r = integration_client.get("/api/attention/raised", headers=_H(_TOKEN))
        assert r.status_code == 200
        ids = [it["external_id"] for it in r.json()["items"]]
        assert aid in ids
        row = next(it for it in r.json()["items"] if it["external_id"] == aid)
        assert row["attention_state"] == "raised"
        assert row["requested_action"] == "approve budget"

    def test_unauthenticated_is_401(self, integration_client):
        assert integration_client.get("/api/attention/raised", headers=_H("bad")).status_code == 401


class TestReviewResult:
    def test_review_surfaces_blocking_failure(self, integration_client):
        aid = _eid()
        _seed(aid, _BAD)
        r = integration_client.get(f"/api/attention/{aid}/review", headers=_H(_TOKEN))
        assert r.status_code == 200
        body = r.json()
        assert body["parent_conversation_context_injected"] is False
        assert body["blocking_failure"] is True
        assert body["may_approve_to_user"] is False


class TestDispositionGate:
    def test_blocking_approve_is_409_then_authorized_override_ok(self, integration_client):
        from infra import conductor
        from infra.database import get_db
        aid = _eid()
        _seed(aid, _BAD)
        blocked = integration_client.post(
            f"/api/attention/{aid}/dispose",
            json={"token": _TOKEN, "outcome": "approve_to_user"})
        assert blocked.status_code == 409  # blocking review failure halts approve
        # A caller-owned but NON-conductor actor: passes actor-access, fails the
        # conductor-authority check → 409 (cannot self-assert override authority).
        db = get_db()
        db.execute("INSERT OR IGNORE INTO threads (id, user_id, title, created_at, updated_at) "
                   "VALUES ('steve-actor','steve','A','t','t')")
        db.execute("INSERT OR IGNORE INTO threads (id, user_id, title, created_at, updated_at) "
                   "VALUES ('ws-cond','steve','C','t','t')")
        db.commit()
        self_assert = integration_client.post(
            f"/api/attention/{aid}/dispose",
            json={"token": _TOKEN, "outcome": "approve_to_user",
                  "override_blocking": True, "authority": "me", "reason": "x",
                  "actor_thread": "steve-actor"})
        assert self_assert.status_code == 409
        # A real workspace conductor as actor is authorized.
        corsia_agent.set_corsia_agent("ws-cond")
        db.close()
        ok = integration_client.post(
            f"/api/attention/{aid}/dispose",
            json={"token": _TOKEN, "outcome": "approve_to_user",
                  "override_blocking": True, "authority": "conductor",
                  "reason": "time-critical", "actor_thread": "ws-cond"})
        assert ok.status_code == 200 and ok.json()["user_visible"] is True

    def test_request_revision_routes_back_not_user_visible(self, integration_client):
        aid = _eid()
        _seed(aid, _GOOD)
        r = integration_client.post(
            f"/api/attention/{aid}/dispose",
            json={"token": _TOKEN, "outcome": "request_revision", "reason": "more detail"})
        assert r.status_code == 200
        assert r.json()["user_visible"] is False


class TestClearing:
    def test_clear_removes_from_raised(self, integration_client):
        aid = _eid()
        _seed(aid, _GOOD)
        r = integration_client.post(
            f"/api/attention/{aid}/clear", json={"token": _TOKEN, "outcome": "answer"})
        assert r.status_code == 200
        body = r.json()
        assert body["artifact_archived"] is False and body["subscription_archived"] is False
        listed = integration_client.get("/api/attention/raised", headers=_H(_TOKEN)).json()
        assert aid not in [it["external_id"] for it in listed["items"]]

    def test_clear_emits_one_product_event_from_the_attention_owner(
        self, integration_client, monkeypatch,
    ):
        from api import attention as api_attention
        from registry import attention as registry_attention

        aid = _eid()
        _seed(aid, _GOOD)
        api_emitted = []
        owner_emitted = []
        monkeypatch.setattr(
            api_attention, "emit_product_event",
            lambda *args, **kwargs: api_emitted.append((args, kwargs)), raising=False,
        )
        monkeypatch.setattr(
            registry_attention, "emit_product_event",
            lambda category, action, *, properties: owner_emitted.append(
                (category, action, properties)
            ),
        )

        response = integration_client.post(
            f"/api/attention/{aid}/clear",
            json={"token": _TOKEN, "outcome": "answer"},
        )

        assert response.status_code == 200
        assert api_emitted == []
        assert [(category, action) for category, action, _props in owner_emitted] == [
            ("artifact_attention", "cleared"),
        ]

    def test_clear_by_external_id_releases_the_real_plan_ask(self, integration_client):
        from infra.database import get_db
        from plans import service
        from plans.boundary import PlanActor, mutate
        from registry import models

        aid = f"ask-{uuid.uuid4().hex[:8]}.md"
        owner = f"owner-{uuid.uuid4().hex[:8]}"
        db = get_db()
        db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (owner, "steve", "Plan owner", "2026-09-29T12:00:00+00:00", "2026-09-29T12:00:00+00:00"),
        )
        db.commit()
        models.register_artifact(
            integration="workspace", type="document", external_id=aid,
            parent_thread_id=owner, title="Review the brief", db=db,
        )
        artifact_id = str(db.execute(
            "SELECT id FROM artifact_identities WHERE external_id=?", (aid,),
        ).fetchone()["id"])
        plan = service.ensure_plan("thread", owner, db=db)
        node = service.add_node(
            plan.id, kind="task", title="Review brief", acceptance_criteria="Reviewed", db=db,
        )
        db.commit()
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Review the brief",
                "owner_kind": "user", "ask_artifact_id": artifact_id,
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=PlanActor(owner, "steve", None), provenance="test.attention",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
        db.close()

        response = integration_client.post(
            f"/api/attention/{aid}/clear",
            json={"token": _TOKEN, "outcome": "acknowledge"},
        )

        assert response.status_code == 200
        db = get_db()
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
            (node.id,),
        ).fetchone()[0] == 0
        db.close()

    def test_defer_requires_condition(self, integration_client):
        aid = _eid()
        _seed(aid, _GOOD)
        bad = integration_client.post(
            f"/api/attention/{aid}/clear", json={"token": _TOKEN, "outcome": "defer"})
        assert bad.status_code == 400
        ok = integration_client.post(
            f"/api/attention/{aid}/clear",
            json={"token": _TOKEN, "outcome": "defer",
                  "condition": {"type": "deadline", "value": "2026-06-27T09:00:00Z"}})
        assert ok.status_code == 200 and ok.json()["attention_state"] == "deferred"


class TestRollupAndConductor:
    def test_rollup_is_pull_only_and_scoped_to_parent(self, integration_client):
        aid = _eid()
        parent = f"parent-{uuid.uuid4().hex[:6]}"
        _seed(aid, _GOOD, parent=parent)
        r = integration_client.get(
            f"/api/attention/rollup?parent_thread_id={parent}", headers=_H(_TOKEN))
        assert r.status_code == 200
        assert r.json()["pull_only"] is True
        assert aid in [it["external_id"] for it in r.json()["items"]]

class TestRaisedScopedToConductor:
    """Regression: GET /api/attention/raised?thread_id=<conductor> returns items
    where the conductor is a subscriber (parent_chain or owner) and excludes items
    it is not subscribed to.  This proves conductorSessionId in the frontend
    correctly scopes the conductor pane to supervised items only."""

    def test_conductor_scoped_raised_includes_subscribed_item(self, integration_client):
        conductor_thr = f"cond-{uuid.uuid4().hex[:8]}"
        aid = _eid()
        # Seed with conductor as parent_chain subscriber (worker thread is owner).
        _seed(aid, _GOOD, owner=f"worker-{uuid.uuid4().hex[:6]}", parent=conductor_thr)
        r = integration_client.get(
            f"/api/attention/raised?thread_id={conductor_thr}", headers=_H(_TOKEN))
        assert r.status_code == 200
        ids = [it["external_id"] for it in r.json()["items"]]
        assert aid in ids, "conductor-subscribed item must appear in scoped raised list"

    def test_conductor_scoped_raised_excludes_unsubscribed_item(self, integration_client):
        conductor_thr = f"cond-{uuid.uuid4().hex[:8]}"
        other_aid = _eid()
        # Seed with NO conductor subscription — different owner, no parent set.
        _seed(other_aid, _GOOD, owner=f"other-{uuid.uuid4().hex[:6]}")
        r = integration_client.get(
            f"/api/attention/raised?thread_id={conductor_thr}", headers=_H(_TOKEN))
        assert r.status_code == 200
        ids = [it["external_id"] for it in r.json()["items"]]
        assert other_aid not in ids, "unsubscribed item must NOT appear in conductor-scoped list"

    def test_conductor_scoped_raised_both_together(self, integration_client):
        """Single request: subscribed item IN, unsubscribed item OUT."""
        conductor_thr = f"cond-{uuid.uuid4().hex[:8]}"
        in_aid = _eid()
        out_aid = _eid()
        _seed(in_aid, _GOOD, owner=f"worker-{uuid.uuid4().hex[:6]}", parent=conductor_thr)
        _seed(out_aid, _GOOD, owner=f"other-{uuid.uuid4().hex[:6]}")
        r = integration_client.get(
            f"/api/attention/raised?thread_id={conductor_thr}", headers=_H(_TOKEN))
        assert r.status_code == 200
        ids = [it["external_id"] for it in r.json()["items"]]
        assert in_aid in ids
        assert out_aid not in ids


class TestAuthorization:
    """#595: token auth alone is insufficient — endpoints must check the caller
    may act on the specific artifact (may_access_artifact) and the actor_thread."""

    _OTHER = OTHER_TOKEN  # a different real user

    def test_other_user_cannot_review_or_clear_or_dispose(self, integration_client):
        aid = _eid()
        _seed(aid, _GOOD)  # owned by a steve-owned thread
        rv = integration_client.get(f"/api/attention/{aid}/review", headers=_H(self._OTHER))
        cl = integration_client.post(f"/api/attention/{aid}/clear",
                                     json={"token": self._OTHER, "outcome": "answer"})
        dp = integration_client.post(f"/api/attention/{aid}/dispose",
                                     json={"token": self._OTHER, "outcome": "request_revision",
                                           "reason": "x"})
        assert rv.status_code == 403 and cl.status_code == 403 and dp.status_code == 403

    def test_other_user_does_not_see_artifact_in_lists(self, integration_client):
        aid = _eid()
        _seed(aid, _GOOD)
        listed = integration_client.get(
            "/api/attention/raised", headers=_H(self._OTHER)).json()["items"]
        assert aid not in [it["external_id"] for it in listed]

    def test_actor_thread_not_accessible_is_403(self, integration_client):
        from infra.database import get_db
        aid = _eid()
        _seed(aid, _GOOD)
        db = get_db()
        db.execute("INSERT OR IGNORE INTO threads (id, user_id, title, created_at, updated_at) "
                   "VALUES ('leksi-thread','leksi','L','t','t')")
        db.commit(); db.close()
        # steve names a thread he does not own as actor_thread → 403.
        r = integration_client.post(f"/api/attention/{aid}/dispose",
                                    json={"token": _TOKEN, "outcome": "answer_internally",
                                          "answer": "ok", "actor_thread": "leksi-thread"})
        assert r.status_code == 403


class TestDbHandleHygiene:
    """#595: the authz helpers close only the DB handle they self-open and leave
    an injected handle usable (no leaked/rolled-back caller connection)."""

    def test_resolve_access_self_open_and_injected(self, isolated_db):
        from api.attention import _resolve_access
        from datetime import datetime, timezone
        from infra.database import get_db
        from registry import models
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT OR IGNORE INTO threads (id, user_id, title, created_at, updated_at) "
                   "VALUES ('o','steve','T',?,?)", (now, now))
        db.commit()
        models.register_artifact(integration="local", type="user-raise",
                                 external_id="ra-1", parent_thread_id="o", db=db)
        db.commit()
        # self-opened (db=None) path resolves correctly + closes its own handle.
        owner, found, ok = _resolve_access("ra-1", "steve")
        assert found and ok and owner == "o"
        # injected handle is NOT closed — still usable after the call.
        owner2, found2, ok2 = _resolve_access("ra-1", "steve", db=db)
        assert found2 and ok2
        assert db.execute("SELECT 1").fetchone() is not None
        db.close()


class TestOwnerlessNotExposed:
    """#595: an ownerless (owner_thread is None) raised artifact must NOT pass the
    authz filter — may_access_artifact(None) is True by sentinel convention."""

    def test_ownerless_raised_item_hidden_and_inaccessible(self, integration_client):
        from registry.ownership import release_artifact
        aid = _eid()
        _seed(aid, _GOOD)  # raised, owned by a steve thread
        # Release ownership → ownerless, but attention_state stays 'raised'.
        release_artifact(integration="local", type="user-raise", external_id=aid,
                         actor_thread_id="owner-thr", authority="test", reason="r",
                         source_path="t")
        listed = integration_client.get(
            "/api/attention/raised", headers=_H(_TOKEN)).json()["items"]
        assert aid not in [it["external_id"] for it in listed]   # not in list
        rv = integration_client.get(f"/api/attention/{aid}/review", headers=_H(_TOKEN))
        assert rv.status_code == 403                              # not accessible
