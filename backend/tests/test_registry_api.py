
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from infra import corsia_agent

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_TOKEN = OWNER_TOKEN
_OTHER_TOKEN = OTHER_TOKEN


def _mk_session(project_id: str | None = None, *, user_id: str = "steve") -> str:
    from infra.database import get_db
    from tests.registry_helpers import make_session

    db = get_db()
    try:
        return make_session(db, project_id=project_id, prefix="rapi", user_id=user_id)
    finally:
        db.close()


def _register(client, sid: str, *, type_: str = "document",
              external_id: str | None = None, **extra):
    body = {
        "integration": "workspace", "type": type_,
        "external_id": external_id or f"art-{uuid.uuid4().hex[:8]}",
        "parent_thread_id": sid, **extra,
    }
    return client.post(f"/api/artifacts/register?token={_TOKEN}", json=body)


def _mk_project() -> str:
    from infra.database import get_db

    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (project_id, f"Project {project_id}", now, now),
        )
        db.commit()
        return project_id
    finally:
        db.close()


def _set_project_conductor(project_id: str, conductor_thread_id: str) -> None:
    from infra import conductor

    conductor.set_scope_coordinator(project_id, conductor_thread_id)


def _set_workspace_conductor(conductor_thread_id: str) -> None:
    from infra import conductor

    corsia_agent.set_corsia_agent(conductor_thread_id)


def _archive_session(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET lifecycle_state = 'archived', "
            "archived_reason = 'user' WHERE id = ?",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.integration
class TestRegisterAndResolve:
    def test_register_then_resolve_roundtrip(self, integration_client):
        sid = _mk_session()
        ext = f"roundtrip-{uuid.uuid4().hex[:8]}"
        r = _register(integration_client, sid, external_id=ext,
                      title="x.md", description="a doc")
        assert r.status_code == 200, r.text
        art = r.json()["artifact"]
        assert art["parent_thread_id"] == sid and art["state"] == "active"

        got = integration_client.get(
            f"/api/artifacts/resolve?token={_TOKEN}&integration=workspace"
            f"&type=document&external_id={ext}")
        assert got.status_code == 200
        assert got.json()["found"] is True
        assert got.json()["artifact"]["id"] == art["id"]

    def test_resolve_by_physical_path_returns_logical_workspace_identity(
        self,
        integration_client,
        integration_workspace,
    ):
        target = integration_workspace / f"resolve-{uuid.uuid4().hex[:8]}.md"
        target.write_text("# Resolve me\n", encoding="utf-8")
        try:
            registered = integration_client.post(
                f"/api/files/register?token={_TOKEN}",
                json={"path": str(target)},
            )
            assert registered.status_code == 200, registered.text
            artifact = registered.json()["artifact"]

            got = integration_client.get(
                "/api/artifacts/resolve",
                params={"token": _TOKEN, "path": str(target)},
            )

            assert got.status_code == 200, got.text
            assert got.json()["found"] is True
            assert got.json()["artifact"]["id"] == artifact["id"]
            assert got.json()["artifact"]["external_id"].startswith("workspace://")
            assert got.json()["artifact"]["path"] == str(target.resolve())
        finally:
            target.unlink(missing_ok=True)

    def test_reregister_preserves_owner(self, integration_client):
        a, b = _mk_session(), _mk_session()
        ext = f"owned-{uuid.uuid4().hex[:8]}"
        first = _register(integration_client, a, external_id=ext).json()["artifact"]
        second = _register(integration_client, b, external_id=ext).json()["artifact"]
        assert second["id"] == first["id"]
        assert second["parent_thread_id"] == a, "registering must not re-home"

    def test_reregister_refreshes_the_identity_and_affected_owner_panes(
        self, integration_client, monkeypatch
    ):
        from infra import state

        a, b = _mk_session(), _mk_session()
        ext = f"refresh-{uuid.uuid4().hex[:8]}"
        first = _register(integration_client, a, external_id=ext).json()["artifact"]
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            state, "broadcast_to_user",
            lambda user_id, message: seen.append((user_id, message)),
        )

        second = _register(integration_client, b, external_id=ext).json()["artifact"]

        assert second["parent_thread_id"] == a
        assert seen == [
            (
                "steve",
                {
                    "type": "artifacts_changed",
                    "artifact_id": first["id"],
                    "owner_thread_id": a,
                    "affected_thread_ids": [a, b],
                },
            )
        ]

    def test_ownerless_reregister_refreshes_all_same_user_subscriber_panes(
        self, integration_client, monkeypatch
    ):
        from infra import state

        owner, opener = _mk_session(), _mk_session()
        ext = f"ownerless-refresh-{uuid.uuid4().hex[:8]}"
        first = _register(integration_client, owner, external_id=ext).json()["artifact"]
        released = integration_client.post(
            f"/api/artifacts/release?token={_TOKEN}",
            json={
                "integration": "workspace",
                "type": "document",
                "external_id": ext,
                "actor_thread_id": owner,
                "authority": "current-owner-handoff",
                "reason": "owner released",
            },
        )
        assert released.status_code == 200, released.text
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            state, "broadcast_to_user",
            lambda user_id, message: seen.append((user_id, message)),
        )

        reopened = _register(integration_client, opener, external_id=ext)

        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["artifact"] is None
        assert len(seen) == 1
        user_id, payload = seen[0]
        assert user_id == "steve"
        assert payload["artifact_id"] == first["id"]
        assert payload["owner_thread_id"] is None
        assert set(payload["affected_thread_ids"]) == {owner, opener}

    def test_identity_refresh_payloads_do_not_disclose_cross_user_threads(
        self, integration_client, monkeypatch
    ):
        from infra import state

        owner = _mk_session(user_id="steve")
        other = _mk_session(user_id="leksi")
        ext = f"cross-user-refresh-{uuid.uuid4().hex[:8]}"
        first = _register(integration_client, owner, external_id=ext).json()["artifact"]
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            state, "broadcast_to_user",
            lambda user_id, message: seen.append((user_id, message)),
        )

        response = integration_client.post(
            f"/api/artifacts/register?token={_OTHER_TOKEN}",
            json={
                "integration": "workspace",
                "type": "document",
                "external_id": ext,
                "parent_thread_id": other,
            },
        )

        assert response.status_code == 200, response.text
        assert {user_id for user_id, _ in seen} == {"steve", "leksi"}
        payloads = {user_id: payload for user_id, payload in seen}
        assert payloads["steve"] == {
            "type": "artifacts_changed",
            "artifact_id": first["id"],
            "owner_thread_id": owner,
            "affected_thread_ids": [owner],
        }
        assert payloads["leksi"] == {
            "type": "artifacts_changed",
            "artifact_id": first["id"],
            "owner_thread_id": None,
            "affected_thread_ids": [other],
        }

    def test_resolve_miss(self, integration_client):
        got = integration_client.get(
            f"/api/artifacts/resolve?token={_TOKEN}&integration=workspace"
            f"&type=document&external_id=never-{uuid.uuid4().hex}")
        assert got.status_code == 200
        assert got.json() == {"found": False, "artifact": None}

    def test_auth_and_ownership_edges(self, integration_client):
        sid = _mk_session()
        assert _register(integration_client, sid).status_code == 200
        r = integration_client.post(
            "/api/artifacts/register?token=wrong",
            json={"integration": "w", "type": "t", "external_id": "e",
                  "parent_thread_id": sid})
        assert r.status_code == 401
        r = _register(integration_client, f"not-a-session-{uuid.uuid4().hex[:6]}")
        assert r.status_code == 404

    def test_out_of_roots_path_is_400(self, integration_client):
        sid = _mk_session()
        r = _register(integration_client, sid, path="/tmp/elsewhere/no.md")
        assert r.status_code == 400
        assert "registrable roots" in r.json()["detail"]

    def test_blank_identity_is_400(self, integration_client):
        sid = _mk_session()
        r = integration_client.post(
            f"/api/artifacts/register?token={_TOKEN}",
            json={"integration": " ", "type": "document", "external_id": "x",
                  "parent_thread_id": sid})
        assert r.status_code == 400


@pytest.mark.integration
class TestProjectArtifacts:
    def _project(self, client) -> dict:
        r = client.post(f"/api/projects?token={_TOKEN}",
                        json={"name": f"rapi-{uuid.uuid4().hex[:8]}"})
        assert r.status_code == 200, r.text
        return r.json()["project"]

    def test_lists_thread_owned_and_sentinel_rows(self, integration_client):
        p = self._project(integration_client)
        sid = _mk_session(p["id"])
        ext = f"proj-doc-{uuid.uuid4().hex[:8]}"
        _register(integration_client, sid, external_id=ext, title="notes.md")

        r = integration_client.get(
            f"/api/projects/{p['id']}/artifacts?token={_TOKEN}&type=document")
        assert r.status_code == 200, r.text
        data = r.json()
        by_title = {a["title"]: a for a in data["artifacts"]}
        assert by_title["notes.md"]["parent_thread_title"] == "owning thread"
        assert {"PROJECT.md", "COORDINATION-MODEL.md"} <= set(by_title)
        assert by_title["PROJECT.md"]["parent_thread_title"] is None
        assert data["total"] >= 3

    def test_filters_and_pagination(self, integration_client):
        p = self._project(integration_client)
        sid = _mk_session(p["id"])
        _register(integration_client, sid, type_="code",
                  external_id=f"c-{uuid.uuid4().hex[:6]}")
        _register(integration_client, sid, type_="upload", retention="ephemeral",
                  external_id=f"u-{uuid.uuid4().hex[:6]}")

        r = integration_client.get(
            f"/api/projects/{p['id']}/artifacts?token={_TOKEN}&type=code")
        types = {a["type"] for a in r.json()["artifacts"]}
        assert types == {"code"}

        r = integration_client.get(
            f"/api/projects/{p['id']}/artifacts?token={_TOKEN}&type=upload")
        assert r.json()["total"] == 0
        r = integration_client.get(
            f"/api/projects/{p['id']}/artifacts?token={_TOKEN}&type=upload&include_ephemeral=true")
        assert r.json()["total"] == 1

        r = integration_client.get(
            f"/api/projects/{p['id']}/artifacts?token={_TOKEN}&limit=1&offset=0")
        assert len(r.json()["artifacts"]) == 1 and r.json()["total"] >= 3

    def test_unknown_project_404_and_bad_token_401(self, integration_client):
        r = integration_client.get(
            f"/api/projects/nope-{uuid.uuid4().hex[:6]}/artifacts?token={_TOKEN}")
        assert r.status_code == 404
        r = integration_client.get("/api/projects/x/artifacts?token=wrong")
        assert r.status_code == 401

    def test_pagination_bounds_validated(self, integration_client):
        p = self._project(integration_client)
        for q in ("limit=-1", "limit=0", "limit=9999", "offset=-5"):
            r = integration_client.get(
                f"/api/projects/{p['id']}/artifacts?token={_TOKEN}&{q}")
            assert r.status_code == 422, q

    def test_register_normalizes_file_paths_and_passes_urls(self, integration_client):
        sid = _mk_session()
        rel = f"working/draft-{uuid.uuid4().hex[:6]}.md"
        r = _register(integration_client, sid, path=rel,
                      external_id=f"norm-{uuid.uuid4().hex[:6]}")
        assert r.status_code == 200, r.text
        art = r.json()["artifact"]
        assert art["path"].startswith("/") and art["path"].endswith(rel.split("/")[-1])
        assert art["external_id"].startswith("workspace://")
        assert art["retention"] == "ephemeral"

        url = "https://github.com/x/y/pull/1"
        r = _register(integration_client, sid, type_="pull-request",
                      external_id=f"pr-{uuid.uuid4().hex[:6]}", path=url)
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["path"] == url


@pytest.mark.integration
class TestArtifactRetention:
    def test_register_rejects_subscriber_durable_promotion(self, integration_client):
        owner = _mk_session()
        subscriber = _mk_session()
        external_id = f"foreign-retention-{uuid.uuid4().hex[:8]}"
        created = _register(
            integration_client,
            owner,
            external_id=external_id,
            retention="ephemeral",
        )
        assert created.status_code == 200, created.text

        rejected = _register(
            integration_client,
            subscriber,
            external_id=external_id,
            retention="durable",
        )

        assert rejected.status_code == 400
        assert rejected.json()["detail"] == (
            "retention can only be changed by the active owner"
        )


@pytest.mark.integration
class TestArtifactDisplayAndReconcile:
    def test_display_hide_and_unhide_change_surfacing_only(self, integration_client):
        sid = _mk_session()
        art = _register(
            integration_client,
            sid,
            external_id=f"display-{uuid.uuid4().hex[:8]}",
            title="display.md",
        ).json()["artifact"]
        original_updated_at = art["updated_at"]

        hidden = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert hidden.status_code == 200, hidden.text
        row = hidden.json()["artifact"]
        assert row["state"] == "active"
        assert row["surfacing"] == "quiet"
        assert row["retention"] == "durable"
        hidden_updated_at = row["updated_at"]
        assert hidden_updated_at > original_updated_at

        from infra.database import get_db

        db = get_db()
        try:
            reg = db.execute(
                "SELECT surfacing, updated_at FROM artifact_registrations "
                "WHERE artifact_id=? AND parent_thread_id=?",
                (art["id"], sid),
            ).fetchone()
            ident = db.execute(
                "SELECT retention, updated_at FROM artifact_identities WHERE id=?",
                (art["id"],),
            ).fetchone()
        finally:
            db.close()
        assert reg["surfacing"] == "quiet"
        assert reg["updated_at"] == hidden_updated_at
        assert ident["retention"] == "durable"
        assert ident["updated_at"] == original_updated_at

        pane = integration_client.get(
            f"/api/threads/{sid}/artifacts?token={_TOKEN}"
        ).json()
        node_ids = {n["id"] for n in pane["nodes"] if n["kind"] == "artifact"}
        assert art["id"] not in node_ids
        assert pane["history"] == []

        shown = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": False},
        )
        assert shown.status_code == 200, shown.text
        shown_row = shown.json()["artifact"]
        assert shown_row["surfacing"] == "visible"
        assert shown_row["updated_at"] > hidden_updated_at

        pane = integration_client.get(
            f"/api/threads/{sid}/artifacts?token={_TOKEN}"
        ).json()
        node_ids = {n["id"] for n in pane["nodes"] if n["kind"] == "artifact"}
        assert art["id"] in node_ids
        assert pane["history"] == []

    def test_pinned_patch_toggles_owner_registration(self, integration_client):
        sid = _mk_session()
        art = _register(
            integration_client,
            sid,
            external_id=f"pinned-{uuid.uuid4().hex[:8]}",
            title="tracker.md",
        ).json()["artifact"]

        pinned = integration_client.patch(
            f"/api/artifacts/{art['id']}/pinned?token={_TOKEN}",
            json={"thread_id": sid, "pinned": True},
        )
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["artifact"]["relationship_state"] == "owner-pinned"

        unpinned = integration_client.patch(
            f"/api/artifacts/{art['id']}/pinned?token={_TOKEN}",
            json={"thread_id": sid, "pinned": False},
        )
        assert unpinned.status_code == 200, unpinned.text
        assert unpinned.json()["artifact"]["relationship_state"] == "owner-active"

    def test_pinned_patch_uses_404_for_foreign_artifact(self, integration_client):
        sid = _mk_session()
        art = _register(integration_client, sid).json()["artifact"]

        denied = integration_client.patch(
            f"/api/artifacts/{art['id']}/pinned?token={_OTHER_TOKEN}",
            json={"thread_id": sid, "pinned": True},
        )

        assert denied.status_code == 404

    def test_artifact_removal_broadcast_reports_delivery_failure(
        self,
        monkeypatch,
    ):
        from infra import state
        from registry.removal_events import broadcast_artifact_removal_event

        def fail_broadcast(*args, **kwargs):
            raise RuntimeError("socket unavailable")

        monkeypatch.setattr(state, "broadcast_to_thread", fail_broadcast)
        assert broadcast_artifact_removal_event({
            "thread_id": "sid",
            "message": {"id": "mid"},
        }) is False

    def test_standalone_removal_event_uses_canonical_write_transaction(
        self, monkeypatch,
    ):
        from unittest.mock import Mock

        from infra.database import get_db, write_transaction
        from registry import removal_events

        sid = _mk_session()
        observed_write_transaction = Mock(wraps=write_transaction)
        monkeypatch.setattr(
            removal_events, "write_transaction", observed_write_transaction,
        )
        monkeypatch.setattr(
            removal_events, "broadcast_artifact_removal_event", Mock(return_value=True),
        )

        payload = removal_events.emit_artifact_removal_event(
            thread_id=sid,
            artifact_id="artifact-id",
            artifact_type="document",
            title="removed.md",
            subtype="hidden_from_pane",
            actor_kind="user",
            hidden=True,
        )

        observed_write_transaction.assert_called_once_with(
            None, scope="artifact_removal_event",
        )
        assert payload["message"]["content"] == "Removed removed.md from pane"

        db = get_db()
        try:
            persisted = db.execute(
                "SELECT content FROM messages WHERE id=?", (payload["message"]["id"],),
            ).fetchone()
        finally:
            db.close()
        assert persisted["content"] == "Removed removed.md from pane"

    def test_display_hide_persists_canonical_removal_event(
        self, integration_client,
    ):
        sid = _mk_session()
        art = _register(
            integration_client,
            sid,
            external_id=f"display-msg-{uuid.uuid4().hex[:8]}",
            title="display-msg.md",
        ).json()["artifact"]

        r = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert r.status_code == 200, r.text

        from infra.database import get_db

        db = get_db()
        try:
            msg = db.execute(
                "SELECT role, content, metadata FROM messages "
                "WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
        finally:
            db.close()
        assert msg["role"] == "system"
        meta = json.loads(msg["metadata"])
        assert meta["type"] == "artifact_removed"
        assert meta["event_type"] == "artifact.hidden_from_pane"
        assert meta["subtype"] == "hidden_from_pane"
        assert meta["artifact_id"] == art["id"]
        assert meta["title"] == "display-msg.md"
        assert meta["hidden"] is True
        assert meta["actor_kind"] == "user"
        assert meta["causal_event_id"] is None

    def test_display_event_insert_failure_rolls_back_surfacing(
        self,
        integration_client,
        monkeypatch,
    ):
        from infra import queries
        from infra.database import get_db
        from registry import models
        from registry.removal_events import emit_artifact_removal_event

        sid = _mk_session()
        art = _register(integration_client, sid).json()["artifact"]
        db = get_db()
        try:
            db.execute("BEGIN")
            row = models.update_surfacing(
                art["id"],
                surfacing=models.SURFACING_QUIET,
                parent_thread_id=sid,
                db=db,
            )

            def fail_insert(*args, **kwargs):
                raise RuntimeError("message insert failed")

            monkeypatch.setattr(queries, "insert_message", fail_insert)
            with pytest.raises(RuntimeError, match="message insert failed"):
                emit_artifact_removal_event(
                    thread_id=sid,
                    artifact_id=art["id"],
                    artifact_type=row["type"],
                    title=row.get("title"),
                    subtype="hidden_from_pane",
                    actor_kind="user",
                    hidden=True,
                    db=db,
                )
        finally:
            db.close()

        db = get_db()
        try:
            surfacing = db.execute(
                "SELECT surfacing FROM artifact_registrations "
                "WHERE artifact_id=? AND parent_thread_id=?",
                (art["id"], sid),
            ).fetchone()["surfacing"]
            event_count = db.execute(
                "SELECT COUNT(*) c FROM messages WHERE thread_id=? "
                "AND json_extract(metadata,'$.type')='artifact_removed'",
                (sid,),
            ).fetchone()["c"]
        finally:
            db.close()
        assert surfacing == models.SURFACING_VISIBLE
        assert event_count == 0

    def test_display_redundant_hide_does_not_duplicate_removal_event(
        self, integration_client,
    ):
        """A no-op hide (already quiet) must not emit a second removal event."""
        sid = _mk_session()
        art = _register(
            integration_client,
            sid,
            external_id=f"display-msg-noop-{uuid.uuid4().hex[:8]}",
            title="noop.md",
        ).json()["artifact"]
        first = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert first.status_code == 200, first.text

        from infra.database import get_db

        db = get_db()
        try:
            before_count = db.execute(
                "SELECT COUNT(*) c FROM messages WHERE thread_id=? "
                "AND json_extract(metadata,'$.type')='artifact_removed'",
                (sid,),
            ).fetchone()["c"]
        finally:
            db.close()
        assert before_count == 1

        second = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert second.status_code == 200, second.text

        db = get_db()
        try:
            after_count = db.execute(
                "SELECT COUNT(*) c FROM messages WHERE thread_id=? "
                "AND json_extract(metadata,'$.type')='artifact_removed'",
                (sid,),
            ).fetchone()["c"]
        finally:
            db.close()
        assert after_count == 1

    def test_display_patch_preserves_404_when_update_returns_null(
        self,
        integration_client,
        monkeypatch,
    ):
        from registry import models

        sid = _mk_session()
        art = _register(integration_client, sid).json()["artifact"]
        monkeypatch.setattr(models, "update_surfacing", lambda *args, **kwargs: None)

        response = integration_client.patch(
            f"/api/artifacts/{art['id']}/display",
            params={"token": _TOKEN},
            json={"thread_id": sid, "hidden": True},
        )

        assert response.status_code == 404

    def test_display_patch_auth_edges(self, integration_client):
        sid = _mk_session()
        art = _register(integration_client, sid).json()["artifact"]
        wrong = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token=bogus-token",
            json={"thread_id": sid, "hidden": True},
        )
        assert wrong.status_code == 401
        missing = integration_client.patch(
            f"/api/artifacts/not-{uuid.uuid4().hex[:6]}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert missing.status_code == 404

    def test_display_patch_rejects_valid_token_for_other_users_artifact(
        self,
        integration_client,
    ):
        sid = _mk_session()
        art = _register(integration_client, sid).json()["artifact"]

        denied = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_OTHER_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert denied.status_code == 404

    def test_display_patch_rejects_sentinel_owned_artifacts(self, integration_client):
        from infra.database import get_db
        from registry import models

        sid = _mk_session()
        db = get_db()
        try:
            art = models.register_artifact(
                integration="workspace",
                type="document",
                external_id=f"sentinel-{uuid.uuid4().hex[:8]}",
                parent_thread_id="ui",
                title="shared.md",
                db=db,
            )
            models.register_artifact(
                integration="workspace",
                type="document",
                external_id=art["external_id"],
                parent_thread_id=sid,
                title="shared.md",
                db=db,
            )
        finally:
            db.close()

        denied = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": sid, "hidden": True},
        )
        assert denied.status_code == 404
        db = get_db()
        try:
            reg = db.execute(
                "SELECT surfacing FROM artifact_registrations "
                "WHERE artifact_id=? AND parent_thread_id=?",
                (art["id"], sid),
            ).fetchone()
        finally:
            db.close()
        assert reg["surfacing"] == "visible"

    def test_display_patch_targets_exact_registration_only(
        self,
        integration_client,
    ):
        """Hiding from one thread's pane must not affect another thread's
        registration of the same artifact."""
        from infra.database import get_db
        from registry import models

        owner = _mk_session()
        subscriber = _mk_session()
        db = get_db()
        try:
            art = models.register_artifact(
                integration="workspace",
                type="document",
                external_id=f"shared-target-{uuid.uuid4().hex[:8]}",
                parent_thread_id=owner,
                title="shared.md",
                db=db,
            )
            models.register_artifact(
                integration="workspace",
                type="document",
                external_id=art["external_id"],
                parent_thread_id=subscriber,
                title="shared.md",
                db=db,
            )
        finally:
            db.close()

        hidden = integration_client.patch(
            f"/api/artifacts/{art['id']}/display?token={_TOKEN}",
            json={"thread_id": subscriber, "hidden": True},
        )
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["artifact"]["parent_thread_id"] == subscriber

        db = get_db()
        try:
            regs = {
                r["parent_thread_id"]: r["surfacing"]
                for r in db.execute(
                    "SELECT parent_thread_id, surfacing FROM artifact_registrations "
                    "WHERE artifact_id=?",
                    (art["id"],),
                )
            }
        finally:
            db.close()
        assert regs[subscriber] == "quiet"
        assert regs[owner] == "visible"

    def test_reconcile_preview_reports_missing_file_artifacts(
        self,
        integration_client,
        integration_workspace,
    ):
        sid = _mk_session()
        ghost = integration_workspace / "working" / f"ghost-{uuid.uuid4().hex[:8]}.md"
        ghost.parent.mkdir(exist_ok=True)
        assert not ghost.exists()
        from infra.database import get_db
        from registry import models

        db = get_db()
        try:
            art = models.register_artifact(
                integration="workspace",
                type="document",
                external_id=str(ghost),
                parent_thread_id=sid,
                title=ghost.name,
                path=str(ghost),
                retention="ephemeral",
                db=db,
            )
        finally:
            db.close()

        r = integration_client.get(
            f"/api/threads/{sid}/artifacts/reconcile?token={_TOKEN}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "preview"
        assert body["dry_run"] is True
        assert body["counts"]["missing"] == 1
        assert body["actions"] == [{
            "action": "tombstone",
            "artifact_id": art["id"],
            "name": ghost.name,
            "path": str(ghost),
            "reason": "file path no longer exists",
        }]

    def test_reconcile_apply_repairs_only_owner_registrations(
        self,
        integration_client,
        integration_workspace,
    ):
        owner = _mk_session()
        subscriber = _mk_session()
        ghost = integration_workspace / "working" / f"ghost-{uuid.uuid4().hex[:8]}.md"
        ghost.parent.mkdir(exist_ok=True)
        assert not ghost.exists()

        from infra.database import get_db
        from registry import models

        db = get_db()
        try:
            artifact = models.register_artifact(
                integration="workspace",
                type="document",
                external_id=str(ghost),
                parent_thread_id=owner,
                title=ghost.name,
                path=str(ghost),
                db=db,
            )
            models.register_artifact(
                integration="workspace",
                type="document",
                external_id=str(ghost),
                parent_thread_id=subscriber,
                title=ghost.name,
                db=db,
            )
            artifact_id = artifact["id"]
        finally:
            db.close()

        subscriber_apply = integration_client.post(
            f"/api/threads/{subscriber}/artifacts/reconcile?token={_TOKEN}"
        )
        assert subscriber_apply.status_code == 200, subscriber_apply.text
        assert subscriber_apply.json()["counts"]["applied"] == 0

        db = get_db()
        try:
            still_active = db.execute(
                "SELECT state FROM artifact_identities WHERE id=?",
                (artifact_id,),
            ).fetchone()
        finally:
            db.close()
        assert still_active["state"] == "active"

        owner_apply = integration_client.post(
            f"/api/threads/{owner}/artifacts/reconcile?token={_TOKEN}"
        )
        assert owner_apply.status_code == 200, owner_apply.text
        applied = owner_apply.json()
        assert applied["mode"] == "apply"
        assert applied["counts"]["applied"] == 1
        assert [
            r["artifact_id"] for r in applied["receipts"] if r["status"] == "applied"
        ] == [artifact_id]

        owner_pane = integration_client.get(
            f"/api/threads/{owner}/artifacts?token={_TOKEN}"
        ).json()
        owner_node_ids = {n["id"] for n in owner_pane["nodes"] if n["kind"] == "artifact"}
        assert artifact_id not in owner_node_ids

    def test_reconcile_apply_is_idempotent_on_replay(
        self,
        integration_client,
        integration_workspace,
    ):
        sid = _mk_session()
        ghost = integration_workspace / "working" / f"ghost-{uuid.uuid4().hex[:8]}.md"
        ghost.parent.mkdir(exist_ok=True)
        from infra.database import get_db
        from registry import models

        db = get_db()
        try:
            art = models.register_artifact(
                integration="workspace",
                type="document",
                external_id=str(ghost),
                parent_thread_id=sid,
                title=ghost.name,
                path=str(ghost),
                retention="ephemeral",
                db=db,
            )
        finally:
            db.close()

        first = integration_client.post(
            f"/api/threads/{sid}/artifacts/reconcile?token={_TOKEN}"
        )
        assert first.status_code == 200, first.text
        assert first.json()["counts"]["applied"] == 1

        second = integration_client.post(
            f"/api/threads/{sid}/artifacts/reconcile?token={_TOKEN}"
        )
        assert second.status_code == 200, second.text
        # Already tombstoned/inactive: no longer a candidate, so no re-applied action.
        assert second.json()["counts"]["applied"] == 0

        db = get_db()
        try:
            identity = db.execute(
                "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
            ).fetchone()
        finally:
            db.close()
        assert identity["state"] == "tombstone"

    def test_reconcile_auth_edges(self, integration_client):
        sid = _mk_session()
        assert integration_client.get(
            f"/api/threads/{sid}/artifacts/reconcile?token=bogus-token"
        ).status_code == 401
        assert integration_client.get(
            f"/api/threads/{sid}/artifacts/reconcile?token={_OTHER_TOKEN}"
        ).status_code == 404
        assert integration_client.get(
            f"/api/threads/not-{uuid.uuid4().hex[:6]}/artifacts/reconcile?token={_TOKEN}"
        ).status_code == 404

    def test_relocate_moves_file_and_preserves_identity(
        self,
        integration_client,
        integration_workspace,
    ):
        sid = _mk_session()
        source = integration_workspace / f"reloc-src-{uuid.uuid4().hex[:8]}.md"
        source.write_text("content")
        target = integration_workspace / f"reloc-dst-{uuid.uuid4().hex[:8]}.md"

        art = _register(
            integration_client, sid, path=str(source),
            external_id=f"reloc-{uuid.uuid4().hex[:8]}",
        ).json()["artifact"]

        r = integration_client.post(
            f"/api/artifacts/{art['id']}/relocate?token={_TOKEN}",
            json={"thread_id": sid, "target_path": str(target)},
        )
        assert r.status_code == 200, r.text
        moved = r.json()["artifact"]
        assert moved["id"] == art["id"]
        assert moved["path"] == str(target.resolve())
        assert not source.exists()
        assert target.exists()

    def test_relocate_rejects_non_owner(
        self,
        integration_client,
        integration_workspace,
    ):
        owner = _mk_session()
        other = _mk_session()
        source = integration_workspace / f"reloc-owner-{uuid.uuid4().hex[:8]}.md"
        source.write_text("content")
        target = integration_workspace / f"reloc-owner-dst-{uuid.uuid4().hex[:8]}.md"

        art = _register(
            integration_client, owner, path=str(source),
            external_id=f"reloc-owner-only-{uuid.uuid4().hex[:8]}",
        ).json()["artifact"]

        r = integration_client.post(
            f"/api/artifacts/{art['id']}/relocate?token={_TOKEN}",
            json={"thread_id": other, "target_path": str(target)},
        )
        assert r.status_code == 400, r.text
        assert source.exists()
        assert not target.exists()

    def test_relocate_rejects_a_non_workspace_artifact_with_400(
        self,
        integration_client,
        integration_workspace,
    ):
        """An upload has no workspace path identity to re-derive.

        Relocating one used to succeed and rewrite it to ``upload``/``code``
        with a ``workspace://`` external id: an unregistered (integration,
        type) that the pane renders as a disabled "No handler is registered"
        row, whose external id no longer matches its own download URL.
        """
        sid = _mk_session()
        source = integration_workspace / f"reloc-up-{uuid.uuid4().hex[:8]}.bin"
        source.write_text("binary-ish")
        target = integration_workspace / f"reloc-up-dst-{uuid.uuid4().hex[:8]}.bin"
        ext = f"upload-{uuid.uuid4().hex[:8]}"

        art = _register(
            integration_client, sid, integration="upload", type_="upload",
            external_id=ext, path=str(source),
        ).json()["artifact"]

        r = integration_client.post(
            f"/api/artifacts/{art['id']}/relocate?token=" + _TOKEN,
            json={"thread_id": sid, "target_path": str(target)},
        )

        assert r.status_code == 400, r.text
        assert "only workspace artifacts" in r.json()["detail"]
        after = integration_client.get(
            f"/api/artifacts/resolve?token={_TOKEN}&integration=upload"
            f"&type=upload&external_id={quote(ext, safe='')}"
        ).json()["artifact"]
        assert after["integration"] == "upload"
        assert after["type"] == "upload"
        assert after["external_id"] == ext
        assert source.exists()
        assert not target.exists()

    def test_relocate_target_collision_is_a_400_not_a_500(
        self,
        integration_client,
        integration_workspace,
    ):
        """A real collision must be rejected by validation, never escape as a
        UNIQUE violation from the identity UPDATE."""
        sid = _mk_session()
        source = integration_workspace / f"reloc-col-{uuid.uuid4().hex[:8]}.md"
        source.write_text("content")
        target = integration_workspace / f"reloc-col-dst-{uuid.uuid4().hex[:8]}.md"
        target.write_text("occupied")

        art = _register(
            integration_client, sid, path=str(source),
            external_id=f"reloc-col-{uuid.uuid4().hex[:8]}",
        ).json()["artifact"]

        r = integration_client.post(
            f"/api/artifacts/{art['id']}/relocate?token=" + _TOKEN,
            json={"thread_id": sid, "target_path": str(target)},
        )

        assert r.status_code == 400, r.text
        assert source.exists()


def _pr_ext() -> str:
    return f"owner/repo-{uuid.uuid4().hex}#1"


def _resolve_pr(client, ext: str):
    return client.get(
        f"/api/artifacts/resolve?token={_TOKEN}&integration=github"
        f"&type=pull-request&external_id={quote(ext, safe='')}"
    )


def _register_pr(client, sid: str, ext: str):
    return _register(
        client, sid, integration="github", type_="pull-request", external_id=ext,
    )


def _release_pr(client, ext: str, *, actor: str, authority="current-owner-handoff",
                reason="owner released", triage: str | None = None):
    body = {
        "integration": "github", "type": "pull-request", "external_id": ext,
        "actor_thread_id": actor, "authority": authority, "reason": reason,
    }
    if triage is not None:
        body["triage_thread_id"] = triage
    return client.post(f"/api/artifacts/release?token={_TOKEN}", json=body)


def _rehome_pr(client, ext: str, *, new_owner: str, actor: str, authority, reason):
    return client.post(
        f"/api/artifacts/rehome?token={_TOKEN}",
        json={
            "integration": "github", "type": "pull-request", "external_id": ext,
            "new_owner_thread_id": new_owner, "actor_thread_id": actor,
            "authority": authority, "reason": reason,
        },
    )


@pytest.mark.integration
class TestArtifactOwnershipTransfer:
    """A2.5 explicit re-home / release authority + projection (HTTP surface)."""

    def test_reregister_reports_current_owner_conflict(self, integration_client):
        a, b = _mk_session(), _mk_session()
        ext = _pr_ext()
        first = _register_pr(integration_client, a, ext).json()["artifact"]

        r = _register_pr(integration_client, b, ext)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["artifact"]["id"] == first["id"]
        assert body["artifact"]["parent_thread_id"] == a
        assert body["ownership"] == {
            "ownership_changed": False,
            "conflict": True,
            "requested_owner_thread_id": b,
            "current_owner_thread_id": a,
            "relationship_state": "subscriber-recent",
        }

    def test_rehome_current_owner_handoff_moves_pr_owner(self, integration_client):
        owner, rescue = _mk_session(), _mk_session()
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=owner,
            authority="current-owner-handoff", reason="rescue worker takeover",
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["artifact"]["parent_thread_id"] == rescue
        assert body["old_owner_thread_id"] == owner
        assert body["new_owner_thread_id"] == rescue
        assert _resolve_pr(integration_client, ext).json()["artifact"][
            "parent_thread_id"
        ] == rescue

    def test_release_current_owner_handoff_clears_pr_owner(self, integration_client):
        owner = _mk_session()
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _release_pr(integration_client, ext, actor=owner)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["old_owner_thread_id"] == owner
        assert body["new_owner_thread_id"] is None
        assert _resolve_pr(integration_client, ext).json() == {
            "found": False, "artifact": None,
        }

    def test_register_after_release_reports_subscriber_not_owner(
        self, integration_client
    ):
        owner, opener = _mk_session(), _mk_session()
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)
        assert _release_pr(integration_client, ext, actor=owner).status_code == 200

        reopened = _register_pr(integration_client, opener, ext)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["artifact"] is None
        assert reopened.json()["ownership"] == {
            "ownership_changed": False,
            "conflict": False,
            "requested_owner_thread_id": opener,
            "current_owner_thread_id": None,
            "relationship_state": "subscriber-recent",
        }

    def test_project_conductor_can_rehome_inside_scope(self, integration_client):
        project_id = _mk_project()
        conductor = _mk_session(project_id)
        owner = _mk_session(project_id)
        rescue = _mk_session(project_id)
        _set_project_conductor(project_id, conductor)
        ext = _pr_ext()
        registered = _register_pr(integration_client, owner, ext)
        assert registered.status_code == 200, registered.text
        assert registered.json()["ownership"]["conflict"] is False
        assert registered.json()["artifact"]["parent_thread_id"] == owner

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=conductor,
            authority="source-scope-conductor", reason="project conductor repair",
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["parent_thread_id"] == rescue

    def test_project_conductor_can_rehome_released_ownerless_inside_scope(
        self, integration_client
    ):
        project_id = _mk_project()
        conductor = _mk_session(project_id)
        owner = _mk_session(project_id)
        rescue = _mk_session(project_id)
        _set_project_conductor(project_id, conductor)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)
        released = _release_pr(integration_client, ext, actor=owner)
        assert released.status_code == 200, released.text
        assert released.json()["artifact"] is None

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=conductor,
            authority="source-scope-conductor",
            reason="project conductor rescue after release",
        )
        assert r.status_code == 200, r.text
        assert r.json()["old_owner_thread_id"] is None
        assert r.json()["artifact"]["parent_thread_id"] == rescue

    def test_project_conductor_cannot_rehome_cross_scope(self, integration_client):
        source_project = _mk_project()
        target_project = _mk_project()
        conductor = _mk_session(source_project)
        owner = _mk_session(source_project)
        rescue = _mk_session(target_project)
        _set_project_conductor(source_project, conductor)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=conductor,
            authority="source-scope-conductor", reason="cross-project should fail",
        )
        assert r.status_code == 403
        assert _resolve_pr(integration_client, ext).json()["artifact"][
            "parent_thread_id"
        ] == owner

    def test_project_conductor_cannot_rehome_released_ownerless_cross_scope(
        self, integration_client
    ):
        source_project = _mk_project()
        target_project = _mk_project()
        conductor = _mk_session(source_project)
        owner = _mk_session(source_project)
        rescue = _mk_session(target_project)
        _set_project_conductor(source_project, conductor)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)
        assert _release_pr(integration_client, ext, actor=owner).status_code == 200

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=conductor,
            authority="source-scope-conductor",
            reason="cross-project ownerless repair should fail",
        )
        assert r.status_code == 403
        assert _resolve_pr(integration_client, ext).json() == {
            "found": False, "artifact": None,
        }

    def test_passive_subscriber_cannot_define_rehome_source_scope(
        self, integration_client
    ):
        source_project = _mk_project()
        target_project = _mk_project()
        owner = _mk_session(source_project)
        subscriber = _mk_session(target_project)
        target_scope_coordinator = _mk_session(target_project)
        rescue = _mk_session(target_project)
        _set_project_conductor(target_project, target_scope_coordinator)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)
        assert _release_pr(integration_client, ext, actor=owner).status_code == 200
        subscribed = _register_pr(integration_client, subscriber, ext)
        assert subscribed.status_code == 200, subscribed.text
        assert subscribed.json()["ownership"]["relationship_state"] == (
            "subscriber-recent"
        )

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=target_scope_coordinator,
            authority="source-scope-conductor",
            reason="subscriber scope cannot rescue source artifact",
        )
        assert r.status_code == 403
        assert _resolve_pr(integration_client, ext).json() == {
            "found": False, "artifact": None,
        }

    def test_source_conductor_rehomes_released_artifact_after_later_subscriber(
        self, integration_client
    ):
        source_project = _mk_project()
        target_project = _mk_project()
        source_conductor = _mk_session(source_project)
        owner = _mk_session(source_project)
        rescue = _mk_session(source_project)
        subscriber = _mk_session(target_project)
        _set_project_conductor(source_project, source_conductor)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)
        assert _release_pr(integration_client, ext, actor=owner).status_code == 200
        assert _register_pr(integration_client, subscriber, ext).status_code == 200

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=source_conductor,
            authority="source-scope-conductor",
            reason="source conductor repairs released artifact",
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["parent_thread_id"] == rescue
        assert r.json()["old_owner_thread_id"] is None

    def test_stale_release_cannot_restore_projection_or_source_authority(
        self, integration_client
    ):
        from infra.database import get_db
        from registry import models

        source_project = _mk_project()
        target_project = _mk_project()
        source_conductor = _mk_session(source_project)
        owner_a = _mk_session(source_project)
        owner_b = _mk_session(target_project)
        attempted_rescue = _mk_session(source_project)
        _set_project_conductor(source_project, source_conductor)
        ext = _pr_ext()
        artifact = _register_pr(integration_client, owner_a, ext).json()["artifact"]
        assert _release_pr(integration_client, ext, actor=owner_a).status_code == 200

        db = get_db()
        try:
            models.rehome_artifact(
                integration="github",
                type="pull-request",
                external_id=ext,
                new_owner_thread_id=owner_b,
                actor_thread_id=owner_a,
                authority="test-setup",
                reason="new owner supersedes the earlier release",
                source_path="test",
                db=db,
            )
            db.execute(
                "DELETE FROM artifact_registrations "
                "WHERE artifact_id=? AND parent_thread_id=?",
                (artifact["id"], owner_b),
            )
            db.commit()

            assert models.get_artifact(artifact["id"], db=db) is None
            assert models.list_for_thread(
                owner_a, include_archived=True, db=db
            ) == []
        finally:
            db.close()

        rejected = _rehome_pr(
            integration_client,
            ext,
            new_owner=attempted_rescue,
            actor=source_conductor,
            authority="source-scope-conductor",
            reason="stale release must not restore source authority",
        )
        assert rejected.status_code == 403, rejected.text

    def test_workspace_corsia_can_rehome_cross_scope(self, integration_client):
        source_project = _mk_project()
        target_project = _mk_project()
        corsia = _mk_session()
        owner = _mk_session(source_project)
        rescue = _mk_session(target_project)
        _set_workspace_conductor(corsia)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=corsia,
            authority="workspace-corsia", reason="workspace-level rescue",
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["parent_thread_id"] == rescue

    def test_rehome_rejects_archived_new_owner(self, integration_client):
        project_id = _mk_project()
        conductor = _mk_session(project_id)
        owner = _mk_session(project_id)
        rescue = _mk_session(project_id)
        _set_project_conductor(project_id, conductor)
        _archive_session(rescue)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _rehome_pr(
            integration_client, ext, new_owner=rescue, actor=conductor,
            authority="source-scope-conductor",
            reason="archived target should not receive ownership",
        )
        assert r.status_code == 404
        assert _resolve_pr(integration_client, ext).json()["artifact"][
            "parent_thread_id"
        ] == owner

    def test_release_cannot_triage_to_another_project_by_project_conductor(
        self, integration_client
    ):
        source_project = _mk_project()
        target_project = _mk_project()
        conductor = _mk_session(source_project)
        owner = _mk_session(source_project)
        triage = _mk_session(target_project)
        _set_project_conductor(source_project, conductor)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _release_pr(
            integration_client, ext, actor=conductor,
            authority="source-scope-conductor",
            reason="cross-project triage should fail", triage=triage,
        )
        assert r.status_code == 403
        assert _resolve_pr(integration_client, ext).json()["artifact"][
            "parent_thread_id"
        ] == owner

    def test_current_owner_handoff_cannot_triage_to_another_project(
        self, integration_client
    ):
        """Spec 1.3b: even a current owner cannot release cross-project to triage.

        The current-owner-handoff path must enforce ``_same_scope(source,
        triage)`` like the conductor authorities — otherwise a project-X owner
        could route its artifact's events into another project's triage thread
        (cross-project routing leak). The suite previously only covered the
        conductor path for this rejection.
        """
        source_project = _mk_project()
        target_project = _mk_project()
        owner = _mk_session(source_project)
        triage = _mk_session(target_project)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _release_pr(
            integration_client, ext, actor=owner,
            authority="current-owner-handoff",
            reason="owner cross-project triage should fail", triage=triage,
        )
        assert r.status_code == 403
        # Ownership unchanged — the leak was rejected, not partially applied.
        assert _resolve_pr(integration_client, ext).json()["artifact"][
            "parent_thread_id"
        ] == owner

    def test_current_owner_handoff_can_triage_same_project(
        self, integration_client
    ):
        """Same-project current-owner release-to-triage still succeeds."""
        project_id = _mk_project()
        owner = _mk_session(project_id)
        triage = _mk_session(project_id)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _release_pr(
            integration_client, ext, actor=owner,
            authority="current-owner-handoff",
            reason="owner releases to in-scope triage", triage=triage,
        )
        assert r.status_code == 200, r.text
        assert r.json()["triage_thread_id"] == triage

    def test_release_rejects_archived_triage_target(self, integration_client):
        project_id = _mk_project()
        conductor = _mk_session(project_id)
        owner = _mk_session(project_id)
        triage = _mk_session(project_id)
        _set_project_conductor(project_id, conductor)
        _archive_session(triage)
        ext = _pr_ext()
        _register_pr(integration_client, owner, ext)

        r = _release_pr(
            integration_client, ext, actor=conductor,
            authority="source-scope-conductor",
            reason="archived triage should not receive wakes", triage=triage,
        )
        assert r.status_code == 404
        assert _resolve_pr(integration_client, ext).json()["artifact"][
            "parent_thread_id"
        ] == owner
