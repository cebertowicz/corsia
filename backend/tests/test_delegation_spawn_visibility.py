from __future__ import annotations

import json

from delegation_fixtures import _TOKEN, client, deleg_db  # noqa: F401


class TestSpawnVisibility:
    def _seed_spawner(self, db, sid):
        db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES (?, 'u-test', 'Spawner', '2026-01-01', '2026-01-01')",
            (sid,),
        )
        db.commit()

    def test_spawned_by_persists_for_subagent(self, client, deleg_db, monkeypatch):
        self._seed_spawner(deleg_db, "parent-1")
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Worker A", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent", "spawned_by": "parent-1"},
        )
        assert r.status_code == 200, r.text
        child_id = r.json()["thread_id"]

        row = deleg_db.execute(
            "SELECT spawned_by FROM threads WHERE id = ?", (child_id,)
        ).fetchone()
        assert row is not None
        assert row["spawned_by"] == "parent-1"

    def test_visible_thread_parent_preserved_as_lineage_not_spawned_by(
            self, client, deleg_db, monkeypatch):
        """Visibility derives solely from ``spawned_by`` now, so a non-subagent
        chat thread must NOT carry it — otherwise it would be silently hidden
        from the sidebar. A visible peer may legitimately have a parent: that
        lineage is preserved in
        ``parent_session_id`` (ownership-only, does NOT hide) while
        ``spawned_by`` stays NULL so the thread remains visible. (To create a
        hidden child, use ``thread_kind=subagent``.)"""
        self._seed_spawner(deleg_db, "parent-2")
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Peer B", "model": "anthropic/claude-3-5-opus",
                  "thread_kind": "chat", "spawned_by": "parent-2"},
        )
        assert r.status_code == 200, r.text
        child_id = r.json()["thread_id"]

        row = deleg_db.execute(
            "SELECT spawned_by, parent_thread_id FROM threads WHERE id = ?",
            (child_id,),
        ).fetchone()
        assert row["spawned_by"] is None              # visible peer (not a subagent)
        assert row["parent_thread_id"] == "parent-2"  # provenance preserved

    def test_visible_thread_unowned_parent_rejected(self, client, deleg_db):
        """Re-homing a visible thread's spawned_by → parent_session_id must STILL
        validate ownership (it bypasses create_visible_fresh_thread's guard): a
        peer may not point its lineage at a foreign-user session."""
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES ('foreign-parent', 'u-other', 'Other', '2026-01-01', '2026-01-01')"
        )
        deleg_db.commit()

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Peer C", "model": "anthropic/claude-3-5-opus",
                  "thread_kind": "chat", "spawned_by": "foreign-parent"},
        )
        assert r.status_code == 403
        assert "spawned_by" in r.json()["detail"]
        rows = deleg_db.execute(
            "SELECT id FROM threads WHERE title = 'Peer C'"
        ).fetchall()
        assert rows == []  # not created

    def test_spawn_chip_inserted_in_spawner(self, client, deleg_db, monkeypatch):
        self._seed_spawner(deleg_db, "parent-3")
        broadcasts = []
        monkeypatch.setattr(
            "infra.state.broadcast_to_thread",
            lambda sid, payload: broadcasts.append((sid, payload)),
        )

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Chip Worker", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent", "spawned_by": "parent-3"},
        )
        assert r.status_code == 200, r.text

        chip_rows = deleg_db.execute(
            "SELECT role, metadata FROM messages WHERE thread_id = 'parent-3'"
        ).fetchall()
        assert len(chip_rows) == 1
        row = chip_rows[0]
        assert row["role"] == "system"
        meta = json.loads(row["metadata"])
        assert meta["type"] == "subagent_ref"
        assert meta["provider"] == "native"  # anthropic/claude-3-5-sonnet → native (#551 fix)
        assert meta["thread_kind"] == "subagent"

        assert any(sid == "parent-3" for sid, _ in broadcasts)
        live_row = next(payload["message"] for sid, payload in broadcasts
                        if sid == "parent-3" and payload["type"] == "message_added")
        persisted = deleg_db.execute(
            "SELECT seq, created_at FROM messages WHERE id = ?", (live_row["id"],)
        ).fetchone()
        assert live_row.get("seq") == persisted["seq"] > 0
        assert live_row["created_at"] == persisted["created_at"]

    def test_no_chip_without_spawned_by(self, client, deleg_db, monkeypatch):
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)
        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Standalone", "model": "anthropic/claude-3-5-sonnet"},
        )
        assert r.status_code == 200, r.text
        child_id = r.json()["thread_id"]

        chip_rows = deleg_db.execute(
            "SELECT id FROM messages WHERE role = 'system'"
        ).fetchall()
        assert len(chip_rows) == 0

        row = deleg_db.execute(
            "SELECT spawned_by FROM threads WHERE id = ?", (child_id,)
        ).fetchone()
        assert row["spawned_by"] is None

    def test_403_when_spawned_by_not_owned(self, client, deleg_db):
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
            "VALUES ('other-sess', 'u-other', 'Other', '2026-01-01', '2026-01-01')"
        )
        deleg_db.commit()

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "W", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent", "spawned_by": "other-sess"},
        )
        assert r.status_code == 403
        assert "spawned_by" in r.json()["detail"]

    def test_403_when_spawned_by_nonexistent(self, client, deleg_db):
        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "W", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent", "spawned_by": "does-not-exist"},
        )
        assert r.status_code == 403

    def test_422_subagent_with_no_parent_rejected_loud(self, client, deleg_db):
        """thread_kind=subagent with no spawned_by and no from_thread_id is an
        invisible orphan (issue #536) — must fail loud, not silently create it."""
        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Orphan", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent"},
        )
        assert r.status_code == 422
        assert "536" in r.json()["detail"] or "spawned_by" in r.json()["detail"]

    def test_from_thread_id_auto_fills_spawned_by_for_subagent(
            self, client, deleg_db, monkeypatch):
        """When spawned_by is absent but from_thread_id is provided and owned,
        it is promoted to spawned_by so the sub is never parentless."""
        self._seed_spawner(deleg_db, "caller-1")
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Auto-linked sub", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent", "from_thread_id": "caller-1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["spawned_by"] == "caller-1"

        row = deleg_db.execute(
            "SELECT spawned_by FROM threads WHERE id = ?",
            (body["thread_id"],),
        ).fetchone()
        assert row["spawned_by"] == "caller-1"

        chip = deleg_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = 'caller-1' AND role = 'system'"
        ).fetchone()
        assert chip is not None
        meta = json.loads(chip["metadata"])
        assert meta["type"] == "subagent_ref"

    def test_subagent_inherits_parent_project_when_project_omitted(
            self, client, deleg_db, monkeypatch):
        deleg_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES ('proj-parent', 'Parent Project', '2026-01-01', '2026-01-01')"
        )
        deleg_db.execute(
            "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
            "VALUES ('scoped-parent', 'u-test', 'Scoped Parent', 'proj-parent', "
            "'2026-01-01', '2026-01-01')"
        )
        deleg_db.commit()
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={
                "title": "Scoped child",
                "model": "anthropic/claude-3-5-sonnet",
                "thread_kind": "subagent",
                "from_thread_id": "scoped-parent",
            },
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["project_id"] == "proj-parent"
        row = deleg_db.execute(
            "SELECT project_id FROM threads WHERE id = ?",
            (body["thread_id"],),
        ).fetchone()
        assert row["project_id"] == "proj-parent"

    def test_spawned_by_takes_precedence_over_from_thread_id(
            self, client, deleg_db, monkeypatch):
        """Explicit spawned_by wins over from_thread_id — both present."""
        self._seed_spawner(deleg_db, "explicit-parent")
        self._seed_spawner(deleg_db, "caller-2")
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)

        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={"title": "Explicit wins", "model": "anthropic/claude-3-5-sonnet",
                  "thread_kind": "subagent",
                  "spawned_by": "explicit-parent",
                  "from_thread_id": "caller-2"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["spawned_by"] == "explicit-parent"
