from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
class TestContextBootloaderManagement:
    def test_http_context_change_cannot_claim_task_actor(self, integration_client):
        actor = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": _unique("ctx-claimed-actor")},
        )
        target = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": _unique("ctx-claimed-target")},
        )
        assert actor.status_code == target.status_code == 200

        changed = integration_client.post(
            f"/api/context/bootloader/file?token={_TOKEN}",
            json={
                "scope": "thread",
                "thread_id": target.json()["thread_id"],
                "actor_session_id": actor.json()["thread_id"],
                "path": "claimed-actor.md",
                "content": "# claimed actor\n",
            },
        )

        assert changed.status_code == 422

    def test_workspace_create_and_legacy_global_remove_share_settings_source_truth(self, integration_client):
        from infra.database import get_db
        from infra.time import utc_now_iso

        db = get_db()
        try:
            original_setting = db.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("workspace_context",),
            ).fetchone()
            original_value = original_setting["value"] if original_setting else None
            workspace_setting = db.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("workspace_context",),
            ).fetchone()
            original_workspace_value = workspace_setting["value"] if workspace_setting else None
        finally:
            db.close()

        name = f"context/{_unique('workspace')}.md"
        try:
            created = integration_client.post(
                f"/api/context/bootloader/file?token={_TOKEN}",
                json={"scope": "workspace", "path": name, "content": "# workspace\n"},
            )
            assert created.status_code == 200, created.text
            created_body = created.json()
            assert created_body["scope"] == "workspace"
            assert created_body["stored_path"].endswith(name)

            settings = integration_client.get(f"/api/settings?token={_TOKEN}")
            assert settings.status_code == 200, settings.text
            rows = settings.json()["workspace_context_files"]
            assert any(row["path"] == created_body["stored_path"] for row in rows)

            remaining = [
                row["path"]
                for row in rows
                if row["path"] != created_body["stored_path"]
            ]
            removed = integration_client.patch(
                f"/api/context/bootloader?token={_TOKEN}",
                json={"scope": "global", "files": remaining},
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["scope"] == "workspace"
            assert created_body["stored_path"] not in removed.json()["files"]
        finally:
            db = get_db()
            try:
                if original_value is None:
                    db.execute("DELETE FROM settings WHERE key = ?", ("workspace_context",))
                else:
                    db.execute(
                        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value, updated_at = excluded.updated_at",
                        ("workspace_context", original_value, utc_now_iso()),
                    )
                if original_workspace_value is None:
                    db.execute("DELETE FROM settings WHERE key = ?", ("workspace_context",))
                else:
                    db.execute(
                        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value, updated_at = excluded.updated_at",
                        ("workspace_context", original_workspace_value, utc_now_iso()),
                    )
                db.commit()
            finally:
                db.close()

    def test_project_create_writes_canonical_directory_context(self, integration_client):
        project = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique("ctx-project")},
        )
        assert project.status_code == 200, project.text
        pid = project.json()["project"]["id"]
        project_path = Path(project.json()["project"]["path"])

        created = integration_client.post(
            f"/api/context/bootloader/file?token={_TOKEN}",
            json={
                "scope": "project",
                "project_id": pid,
                "path": "NOTES.md",
                "content": "# project ctx\n",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["stored_path"] == "context/NOTES.md"
        assert created.json()["path"] == str(
            (project_path / "context" / "NOTES.md").resolve()
        )
        assert created.json()["files"] == [
            "context/PROJECT.md",
            "context/NOTES.md",
            "context/COORDINATION-MODEL.md",
        ]

        detail = integration_client.get(f"/api/projects/{pid}?token={_TOKEN}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["context_files"] == created.json()["files"]
        assert json.loads(detail.json()["bootloader_files"]) == []

    def test_project_attach_rejects_false_arbitrary_selection(self, integration_client):
        project = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique("ctx-project-attach")},
        ).json()["project"]
        candidate = Path(project["path"]) / "shared" / "candidate.md"
        candidate.write_text("not canonical")

        attached = integration_client.post(
            f"/api/context/bootloader/attach?token={_TOKEN}",
            json={
                "scope": "project",
                "project_id": project["id"],
                "path": str(candidate),
            },
        )

        assert attached.status_code == 400
        assert "context/" in attached.json()["detail"]

    def test_thread_create_attaches_without_retiring_any_continuation(
        self,
        integration_client,
        monkeypatch,
    ):
        """Task 3: a bootloader change notifies; it never busts the cache.

        The retirement is asserted absent at its own source rather than at the
        API's import, so re-adding it by any route fails this test.
        """
        import thread.runtime_boundary as runtime_boundary
        from infra.database import get_db

        session = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": _unique("ctx-thread")},
        )
        assert session.status_code == 200, session.text
        sid = session.json()["thread_id"]
        retirements: list[str] = []

        async def _fake_retire(thread_id: str, **kwargs):
            retirements.append(thread_id)
            return {}

        monkeypatch.setattr(
            runtime_boundary,
            "retire_harness_continuations",
            _fake_retire,
        )

        created = integration_client.post(
            f"/api/context/bootloader/file?token={_TOKEN}",
            json={
                "scope": "thread",
                "thread_id": sid,
                "path": "thread-context.md",
                "content": "# thread ctx\n",
            },
        )
        assert created.status_code == 200, created.text
        stored = created.json()["stored_path"]
        assert Path(stored).is_absolute()
        # Thread bootloader files root at .threads/{id}/context (durable,
        # dir-scanned) — not the legacy working/{id} scratch.
        assert str(Path(".threads") / sid / "context" / "thread-context.md") in stored
        assert retirements == []

        db = get_db()
        try:
            row = db.execute(
                "SELECT bootloader_files FROM threads WHERE id = ?",
                (sid,),
            ).fetchone()
            events = db.execute(
                "SELECT scope, target_id, action, files_json FROM context_bootloader_events "
                "WHERE scope = 'thread' AND target_id = ?",
                (sid,),
            ).fetchall()
            changes = db.execute(
                "SELECT object_kind,object_id,change_kind,summary,details_json "
                "FROM context_change_journal WHERE object_kind='thread_context' "
                "AND object_id=? ORDER BY seq",
                (sid,),
            ).fetchall()
        finally:
            db.close()

        assert stored in json.loads(row["bootloader_files"])
        assert len(changes) == 1
        assert changes[0]["change_kind"] == "create"
        assert changes[0]["summary"] == "Thread context updated"
        assert stored in json.loads(changes[0]["details_json"])["files"]
        assert events
        assert events[-1]["action"] == "create"
        assert stored in json.loads(events[-1]["files_json"])

    def test_invalid_bootloader_paths_fail_loud(self, integration_client):
        session = integration_client.post(
            f"/api/threads?token={_TOKEN}",
            json={"title": _unique("ctx-invalid")},
        )
        assert session.status_code == 200, session.text
        sid = session.json()["thread_id"]

        for bad in ("/etc/passwd", "../escape.md", "script.py"):
            resp = integration_client.post(
                f"/api/context/bootloader/attach?token={_TOKEN}",
                json={"scope": "thread", "thread_id": sid, "path": bad},
            )
            assert resp.status_code == 400, f"{bad}: {resp.text}"
