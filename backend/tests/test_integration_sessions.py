
from __future__ import annotations

import uuid

import pytest
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN

_TOKEN = OWNER_TOKEN


@pytest.mark.integration
class TestSessionCreateEffort:

    def test_create_session_with_effort_persists(self, integration_client):
        resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Effort session", "reasoning_effort": "high"},
        )
        assert resp.status_code == 200
        sid = resp.json()["thread_id"]

        sessions = integration_client.get(f"/api/threads?token={_TOKEN}").json()["threads"]
        row = next((s for s in sessions if s["id"] == sid), None)
        assert row is not None
        assert row["reasoning_effort"] == "high"

    def test_create_session_without_effort_is_null(self, integration_client):
        resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "No effort session"},
        )
        assert resp.status_code == 200
        sid = resp.json()["thread_id"]

        sessions = integration_client.get(f"/api/threads?token={_TOKEN}").json()["threads"]
        row = next((s for s in sessions if s["id"] == sid), None)
        assert row is not None
        assert row.get("reasoning_effort") is None


@pytest.mark.integration
class TestSessionCreate:

    def test_create_session_returns_session_id(self, integration_client):
        resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Integration test session"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "thread_id" in data
        assert isinstance(data["thread_id"], str)
        assert len(data["thread_id"]) > 0

    def test_create_session_requires_auth(self, integration_client):
        resp = integration_client.post(
            "/api/threads?token=invalid-token",
            json={"title": "Should fail"},
        )
        assert resp.status_code == 401


@pytest.mark.integration
class TestSessionMetadataPatch:

    def test_patch_updates_and_clears_ordinary_fields(self, integration_client):
        from infra.database import get_db

        created = integration_client.post(
            f"/api/threads?token={_TOKEN}", json={"title": "Before patch"}
        )
        sid = created.json()["thread_id"]
        updated = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={"title": "After patch", "reasoning_effort": "high", "pinned": True},
        )
        assert updated.status_code == 200

        cleared = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}",
            json={"title": "", "reasoning_effort": None, "pinned": False},
        )
        assert cleared.status_code == 200
        db = get_db()
        try:
            row = db.execute(
                "SELECT title, reasoning_effort, pinned FROM threads WHERE id = ?",
                (sid,),
            ).fetchone()
        finally:
            db.close()
        assert tuple(row) == ("After patch", None, 0)


@pytest.mark.integration
class TestSessionList:

    def test_list_sessions_returns_wrapper(self, integration_client):
        resp = integration_client.get(f"/api/threads?token={_TOKEN}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "threads" in data
        assert isinstance(data["threads"], list)

    def test_list_sessions_includes_created(self, integration_client):
        create_resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Listed session"},
        )
        sid = create_resp.json()["thread_id"]

        list_resp = integration_client.get(f"/api/threads?token={_TOKEN}")
        sessions = list_resp.json()["threads"]
        ids = [s["id"] for s in sessions]
        assert sid in ids

    def test_list_sessions_reads_every_row_in_one_packed_step(self, integration_client, monkeypatch):
        """The sidebar read steps once, and returns exactly what a per-row fetch returns."""
        from api import threads as threads_api
        from infra.database import get_db

        sids = [
            integration_client.post(f"/api/threads?token={_TOKEN}", json={"title": title}).json()["thread_id"]
            for title in ("Packed one", 'Packed "two" é 😀')
        ]
        for content in ("first", "latest " + "x" * 120):
            integration_client.post(
                f"/api/threads/{sids[0]}/system-message?token={_TOKEN}", json={"content": content},
            )
        reads = []
        packed = threads_api.fetch_all

        def recording(db, sql, params=()):
            reads.append((sql, list(params)))
            return packed(db, sql, params)

        monkeypatch.setattr(threads_api, "fetch_all", recording)
        listed = integration_client.get(f"/api/threads?token={_TOKEN}&archived=1").json()["threads"]

        assert len(reads) == 1
        db = get_db()
        try:
            rows = db.execute(*reads[0]).fetchall()
        finally:
            db.close()
        expected = []
        for row in rows:
            item = dict(row)
            item["archived"] = 1 if row["lifecycle_state"] == "archived" and row["archived_reason"] == "user" else 0
            expected.append(item)
        assert [list(item.items()) for item in listed] == [list(item.items()) for item in expected]
        by_id = {item["id"]: item for item in listed}
        assert by_id[sids[0]]["last_message"] == ("latest " + "x" * 120)[:100]
        assert by_id[sids[1]]["title"] == 'Packed "two" é 😀'

    def test_list_sessions_requires_auth(self, integration_client):
        resp = integration_client.get("/api/threads?token=bad-token")
        assert resp.status_code == 401

    def test_list_sessions_derives_legacy_archived_shape_from_canonical_lifecycle(
        self, integration_client
    ):
        from infra.database import get_db

        ids = {}
        for state in ("active", "dormancy", "unknown-archive", "user", "purged"):
            response = integration_client.post(
                f"/api/threads?token={_TOKEN}",
                json={"title": f"Lifecycle {state}"},
            )
            ids[state] = response.json()["thread_id"]

        db = get_db()
        try:
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason='dormancy', archived_at='a0' WHERE id=?",
                (ids["dormancy"],),
            )
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason='user', archived_at='a0' WHERE id=?",
                (ids["user"],),
            )
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason=NULL, archived_at='a0' WHERE id=?",
                (ids["unknown-archive"],),
            )
            db.execute(
                "UPDATE threads SET lifecycle_state='purged', purged_at='p0' WHERE id=?",
                (ids["purged"],),
            )
            db.commit()
        finally:
            db.close()

        default_rows = {
            row["id"]: row
            for row in integration_client.get(
                f"/api/threads?token={_TOKEN}"
            ).json()["threads"]
        }
        assert ids["active"] in default_rows
        assert ids["dormancy"] in default_rows
        assert ids["unknown-archive"] in default_rows
        assert ids["user"] not in default_rows
        assert ids["purged"] not in default_rows
        assert default_rows[ids["dormancy"]]["archived"] == 0

        all_rows = {
            row["id"]: row
            for row in integration_client.get(
                f"/api/threads?token={_TOKEN}&archived=1"
            ).json()["threads"]
        }
        assert ids["user"] in all_rows
        assert all_rows[ids["user"]]["archived"] == 1
        assert ids["purged"] not in all_rows


@pytest.mark.integration
class TestSessionArchiveLifecycle:
    def _create(self, integration_client, title: str) -> str:
        response = integration_client.post(
            f"/api/threads?token={_TOKEN}", json={"title": title}
        )
        assert response.status_code == 200
        return response.json()["thread_id"]

    def _lifecycle(self, thread_id: str):
        from infra.database import get_db

        db = get_db()
        try:
            return db.execute(
                "SELECT lifecycle_state,dormant_at,archived_at,archived_reason "
                "FROM threads WHERE id=?",
                (thread_id,),
            ).fetchone()
        finally:
            db.close()

    def test_patch_archives_and_unarchives_canonical_lifecycle_synchronously(
        self, integration_client
    ):
        sid = self._create(integration_client, "Archive transition")

        archived = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": True}
        )
        assert archived.status_code == 200
        row = self._lifecycle(sid)
        assert row["lifecycle_state"] == "archived"
        assert row["archived_reason"] == "user"
        assert row["archived_at"]

        unarchived = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": False}
        )
        assert unarchived.status_code == 200
        assert tuple(self._lifecycle(sid)) == ("active", None, None, None)

    def test_patch_false_does_not_unarchive_dormancy_archive(
        self, integration_client
    ):
        from infra.database import get_db

        sid = self._create(integration_client, "Dormancy archive")
        db = get_db()
        try:
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason='dormancy', archived_at='a0' WHERE id=?",
                (sid,),
            )
            db.commit()
        finally:
            db.close()

        response = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": False}
        )
        assert response.status_code == 200
        row = self._lifecycle(sid)
        assert (row["lifecycle_state"], row["archived_reason"], row["archived_at"]) == (
            "archived",
            "dormancy",
            "a0",
        )

    def test_patch_rejects_purged_thread(self, integration_client):
        from infra.database import get_db

        sid = self._create(integration_client, "Purged thread")
        db = get_db()
        try:
            db.execute(
                "UPDATE threads SET lifecycle_state='purged', purged_at='p0' WHERE id=?",
                (sid,),
            )
            db.commit()
        finally:
            db.close()

        response = integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": False}
        )
        assert response.status_code == 409

    def test_product_event_emits_only_for_actual_user_archive(
        self, integration_client, monkeypatch
    ):
        import api.thread_mutations as sessions_api

        sid = self._create(integration_client, "Archive event")
        events = []
        monkeypatch.setattr(
            sessions_api,
            "emit_product_event",
            lambda *args, **kwargs: events.append((args, kwargs)),
        )
        integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": True}
        )
        integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": True}
        )
        integration_client.patch(
            f"/api/threads/{sid}?token={_TOKEN}", json={"archived": False}
        )
        ended = [event for event in events if event[0][:2] == ("session", "ended")]
        assert len(ended) == 1


@pytest.mark.integration
class TestSessionGet:

    def test_get_messages_for_session(self, integration_client):
        create_resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Message test"},
        )
        sid = create_resp.json()["thread_id"]

        resp = integration_client.get(f"/api/threads/{sid}/messages?token={_TOKEN}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "messages" in data
        assert isinstance(data["messages"], list)

    def test_get_messages_nonexistent_session(self, integration_client):
        resp = integration_client.get(
            f"/api/threads/nonexistent-id/messages?token={_TOKEN}"
        )
        assert resp.status_code == 404


@pytest.mark.integration
class TestSessionMessagePaging:

    def _seed_thread(self, integration_client, n: int) -> str:
        from infra.database import get_db

        sid = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": f"Paging thread ({n})"},
        ).json()["thread_id"]

        db = get_db()
        try:
            for i in range(n):
                role = "user" if i % 2 == 0 else "assistant"
                db.execute(
                    "INSERT INTO messages "
                    "(id, thread_id, role, content, metadata, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"{sid}-{i:04d}", sid, role, f"message {i}", None,
                     f"2026-06-12T00:00:{i:04d}"),
                )
            db.commit()
        finally:
            db.close()
        return sid

    def test_first_page_returns_most_recent_and_flags_more(self, integration_client):
        sid = self._seed_thread(integration_client, 30)
        resp = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&limit=10"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 10
        assert data["messages"][-1]["content"] == "message 29"  # newest last
        assert data["messages"][0]["content"] == "message 20"
        assert data["has_more"] is True
        assert data["oldest_id"] == data["messages"][0]["id"]

    def test_scroll_up_walk_covers_full_history(self, integration_client):
        sid = self._seed_thread(integration_client, 27)
        seen: list[str] = []
        resp = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&limit=10"
        ).json()
        while True:
            seen = [m["content"] for m in resp["messages"]] + seen
            if not resp["has_more"]:
                break
            cursor = resp["oldest_id"]
            resp = integration_client.get(
                f"/api/threads/{sid}/messages?token={_TOKEN}&limit=10&before={cursor}"
            ).json()
        assert seen == [f"message {i}" for i in range(27)]
        assert len(seen) == len(set(seen))  # no dupes across pages

    def test_unknown_before_cursor_returns_empty_not_404(self, integration_client):
        sid = self._seed_thread(integration_client, 5)
        resp = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&limit=10&before=nope-nope"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []
        assert data["has_more"] is False
        assert data["oldest_id"] is None

    def test_exact_fit_no_false_has_more(self, integration_client):
        sid = self._seed_thread(integration_client, 8)
        data = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&limit=8"
        ).json()
        assert len(data["messages"]) == 8
        assert data["has_more"] is False

    def test_before_without_limit_returns_400(self, integration_client):
        sid = self._seed_thread(integration_client, 5)
        first = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&limit=3"
        ).json()
        cursor = first["oldest_id"]
        resp = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&before={cursor}"
        )
        assert resp.status_code == 400

    def test_before_with_zero_limit_returns_400(self, integration_client):
        sid = self._seed_thread(integration_client, 5)
        first = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&limit=3"
        ).json()
        cursor = first["oldest_id"]
        resp = integration_client.get(
            f"/api/threads/{sid}/messages?token={_TOKEN}&before={cursor}&limit=0"
        )
        assert resp.status_code == 400


@pytest.mark.integration
class TestSessionListRoleFilter:

    def _session_ids(self, integration_client) -> list[str]:
        return [s["id"] for s in
                integration_client.get(f"/api/threads?token={_TOKEN}").json()["threads"]]

    def test_normal_chat_session_is_included(self, integration_client):
        resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Normal chat"},
        )
        sid = resp.json()["thread_id"]
        assert sid in self._session_ids(integration_client)

    def test_delegation_subagent_excluded(self, integration_client, monkeypatch):
        monkeypatch.setattr(
            "api.delegation.is_dispatchable_model", lambda _model: True
        )
        monkeypatch.setattr(
            "infra.fresh_threads.provider_label", lambda _model: "claude-code"
        )
        # Create a parent to satisfy the parentless-subagent guard (issue #536).
        parent_resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Subagent parent"},
        )
        parent_id = parent_resp.json()["thread_id"]

        resp = integration_client.post(
            f"/api/delegation/threads?token={_TOKEN}",
            json={"title": "Worker subagent", "thread_kind": "subagent",
                  "model": "claude-code/sonnet", "spawned_by": parent_id},
        )
        assert resp.status_code == 200
        sid = resp.json()["thread_id"]
        assert sid not in self._session_ids(integration_client)

    def test_delegation_visible_chat_included(self, integration_client, monkeypatch):
        monkeypatch.setattr(
            "api.delegation.is_dispatchable_model", lambda _model: True
        )
        monkeypatch.setattr(
            "infra.fresh_threads.provider_label", lambda _model: "claude-code"
        )
        resp = integration_client.post(
            f"/api/delegation/threads?token={_TOKEN}",
            json={"title": "Visible delegated thread", "thread_kind": "chat",
                  "model": "claude-code/sonnet"},
        )
        assert resp.status_code == 200
        sid = resp.json()["thread_id"]
        assert sid in self._session_ids(integration_client)

    def test_workspace_conductor_excluded(self, integration_client):
        create_resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Workspace conductor"},
        )
        sid = create_resp.json()["thread_id"]

        assert sid in self._session_ids(integration_client)

        denied = integration_client.put(
            f"/api/conductor?token={_TOKEN}&scope=workspace",
            json={"thread_id": sid},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == (
            "This change needs the signed-in person: open CORSIA and make it there."
        )

        put_resp = integration_client.put(
            f"/api/conductor?token={OWNER_SESSION_TOKEN}&scope=workspace",
            json={"thread_id": sid},
        )
        assert put_resp.status_code == 200

        try:
            assert sid not in self._session_ids(integration_client)
        finally:
            integration_client.put(
                f"/api/conductor?token={OWNER_SESSION_TOKEN}&scope=workspace",
                json={"thread_id": None},
            )


    def test_project_conductor_excluded(self, integration_client):
        proj_resp = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": f"test-project-conductor-{uuid.uuid4().hex[:8]}", "status": "active"},
        )
        assert proj_resp.status_code == 200
        project_id = proj_resp.json()["project"]["id"]

        create_resp = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": "Per-project conductor", "project_id": project_id},
        )
        sid = create_resp.json()["thread_id"]

        assert sid in self._session_ids(integration_client)

        put_resp = integration_client.put(
            f"/api/conductor?token={OWNER_SESSION_TOKEN}&scope={project_id}",
            json={"thread_id": sid},
        )
        assert put_resp.status_code == 200

        try:
            assert sid not in self._session_ids(integration_client)
        finally:
            integration_client.put(
                f"/api/conductor?token={OWNER_SESSION_TOKEN}&scope={project_id}",
                json={"thread_id": None},
            )
