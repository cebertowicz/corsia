
from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
class TestProjectDetailFreshDB:

    def test_detail_returns_200_without_v1_task_or_notes_fields(self, integration_client):
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name("notes-fix"), "description": "x", "status": "active"},
        )
        assert created.status_code == 200, created.text
        pid = created.json()["project"]["id"]

        resp = integration_client.get(f"/api/projects/{pid}?token={_TOKEN}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "notes" not in data
        assert "tasks" not in data
        assert data["projects_root"] == str(Path(data["path"]).parent)
        assert isinstance(data["conversations"], list)

    def test_list_carries_catalog_fields(self, integration_client):
        name = _unique_name("catalog")
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": name, "description": "x", "accent_color": "#16a34a"},
        )
        assert created.status_code == 200, created.text

        resp = integration_client.get(f"/api/projects?token={_TOKEN}&status=all")
        assert resp.status_code == 200, resp.text
        listed = {p["name"]: p for p in resp.json()["projects"]}[name]
        assert listed["status"] == "active"
        assert listed["accent_color"] == "#16a34a"

    def test_detail_unknown_project_404(self, integration_client):
        resp = integration_client.get(f"/api/projects/does-not-exist-xyz?token={_TOKEN}")
        assert resp.status_code == 404

    def test_duplicate_name_returns_409_not_500(self, integration_client):
        name = _unique_name("dup")
        first = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": name, "status": "active"},
        )
        assert first.status_code == 200, first.text

        again = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": name, "status": "active"},
        )
        assert again.status_code == 409, again.text


@pytest.mark.integration
class TestNullPlanningRoot:

    def test_detail_returns_200_when_legacy_conversion_clears_path(self, integration_client):
        from infra.database import get_db

        created = integration_client.post(
            "/api/projects",
            params={"token": _TOKEN},
            json={"name": _unique_name("cleared-path"), "description": "x"},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["project"]["id"]
        db = get_db()
        try:
            db.execute("UPDATE projects SET path = NULL WHERE id = ?", (project_id,))
            db.commit()
        finally:
            db.close()

        detail = integration_client.get(
            f"/api/projects/{project_id}", params={"token": _TOKEN}
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["path"] is None

    def test_detail_returns_200_for_a_kept_legacy_path(
        self, integration_client, integration_workspace
    ):
        from infra.database import get_db

        created = integration_client.post(
            "/api/projects",
            params={"token": _TOKEN},
            json={"name": _unique_name("kept-path"), "description": "x"},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["project"]["id"]
        legacy = integration_workspace / "legacy-checkout"
        legacy.mkdir()
        db = get_db()
        try:
            db.execute("UPDATE projects SET path = ? WHERE id = ?", (str(legacy), project_id))
            db.commit()
        finally:
            db.close()

        detail = integration_client.get(
            f"/api/projects/{project_id}", params={"token": _TOKEN}
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["path"] == str(legacy)


@pytest.mark.integration
class TestProjectCreateInit:

    def test_create_persists_accent_color(self, integration_client):
        accent = "#16a34a"
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name("accent"), "description": "x", "accent_color": accent},
        )
        assert created.status_code == 200, created.text
        proj = created.json()["project"]
        assert proj["accent_color"] == accent

        detail = integration_client.get(f"/api/projects/{proj['id']}?token={_TOKEN}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["accent_color"] == accent

    def test_create_registers_doc_roots(self, integration_client):
        name = _unique_name("roots")
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": name, "description": "x"},
        )
        assert created.status_code == 200, created.text
        proj = created.json()["project"]

        roots = json.loads(proj["doc_roots"])
        assert isinstance(roots, list) and len(roots) >= 1
        assert roots[0]["path"].endswith(name.lower())
        assert roots[0]["purpose"]

    def test_accent_color_patchable(self, integration_client):
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name("accent-patch"), "accent_color": "#2563eb"},
        )
        assert created.status_code == 200, created.text
        pid = created.json()["project"]["id"]

        patched = integration_client.patch(
            f"/api/projects/{pid}?token={_TOKEN}",
            json={"accent_color": "#dc2626"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["project"]["accent_color"] == "#dc2626"

    def test_rename_to_existing_name_returns_409(self, integration_client):
        a, b = _unique_name("rn-a"), _unique_name("rn-b")
        ra = integration_client.post(f"/api/projects?token={_TOKEN}", json={"name": a})
        rb = integration_client.post(f"/api/projects?token={_TOKEN}", json={"name": b})
        assert ra.status_code == 200 and rb.status_code == 200
        pid_b = rb.json()["project"]["id"]

        clash = integration_client.patch(f"/api/projects/{pid_b}?token={_TOKEN}", json={"name": a})
        assert clash.status_code == 409, clash.text

        same = integration_client.patch(f"/api/projects/{pid_b}?token={_TOKEN}", json={"name": b})
        assert same.status_code == 200, same.text

    def test_accent_reset_to_null(self, integration_client):
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name("reset"), "accent_color": "#7c3aed"},
        )
        assert created.status_code == 200, created.text
        pid = created.json()["project"]["id"]

        reset = integration_client.patch(
            f"/api/projects/{pid}?token={_TOKEN}", json={"accent_color": None}
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["project"]["accent_color"] is None
        assert integration_client.get(f"/api/projects/{pid}?token={_TOKEN}").json()["accent_color"] is None

    _ROOT_GUARD_MESSAGE = (
        "Project path must be an existing folder directly inside the projects folder"
    )

    def _create_project(self, integration_client, prefix):
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name(prefix)},
        )
        assert created.status_code == 200, created.text
        return created.json()["project"]

    def _stored_path(self, project_id):
        from infra.database import get_db

        db = get_db()
        try:
            return db.execute(
                "SELECT path FROM projects WHERE id = ?", (project_id,)
            ).fetchone()["path"]
        finally:
            db.close()

    def test_project_root_patch_rejects_missing_directory(self, integration_client, tmp_path):
        project = self._create_project(integration_client, "root-guard")
        before = self._stored_path(project["id"])

        bad = integration_client.patch(
            f"/api/projects/{project['id']}?token={_TOKEN}",
            json={"path": str(tmp_path / "missing-root")},
        )
        assert bad.status_code == 400, bad.text
        assert bad.json()["detail"] == self._ROOT_GUARD_MESSAGE
        assert self._stored_path(project["id"]) == before

    def test_project_root_patch_rejects_folder_outside_projects(
        self, integration_client, tmp_path
    ):
        project = self._create_project(integration_client, "root-outside")
        before = self._stored_path(project["id"])
        outside = tmp_path / "project-root"
        outside.mkdir()

        bad = integration_client.patch(
            f"/api/projects/{project['id']}?token={_TOKEN}",
            json={"path": str(outside)},
        )
        assert bad.status_code == 400, bad.text
        assert bad.json()["detail"] == self._ROOT_GUARD_MESSAGE
        assert self._stored_path(project["id"]) == before

    def test_project_root_patch_rejects_nested_folder(self, integration_client):
        project = self._create_project(integration_client, "root-nested")
        before = self._stored_path(project["id"])
        nested = Path(project["path"]) / "sub"
        nested.mkdir()

        bad = integration_client.patch(
            f"/api/projects/{project['id']}?token={_TOKEN}",
            json={"path": str(nested)},
        )
        assert bad.status_code == 400, bad.text
        assert self._stored_path(project["id"]) == before

    def test_project_root_patch_rejects_symlink_inside_projects(
        self, integration_client, tmp_path
    ):
        project = self._create_project(integration_client, "root-symlink")
        before = self._stored_path(project["id"])
        target = tmp_path / "elsewhere"
        target.mkdir()
        link = Path(project["path"]).parent / _unique_name("linked-root")
        link.symlink_to(target, target_is_directory=True)

        bad = integration_client.patch(
            f"/api/projects/{project['id']}?token={_TOKEN}",
            json={"path": str(link)},
        )
        assert bad.status_code == 400, bad.text
        assert bad.json()["detail"] == self._ROOT_GUARD_MESSAGE
        assert self._stored_path(project["id"]) == before

    @pytest.mark.parametrize("alias", ["projects-parent", "project-parent"])
    def test_project_root_patch_rejects_parent_aliases(
        self, integration_client, alias
    ):
        project = self._create_project(integration_client, "root-parent-alias")
        before = self._stored_path(project["id"])
        root = Path(project["path"])
        candidate = root.parent / ".." if alias == "projects-parent" else root / ".."

        bad = integration_client.patch(
            f"/api/projects/{project['id']}",
            params={"token": _TOKEN},
            json={"path": str(candidate)},
        )

        assert bad.status_code == 400, bad.text
        assert bad.json()["detail"] == self._ROOT_GUARD_MESSAGE
        assert self._stored_path(project["id"]) == before

    def test_project_root_patch_stores_canonical_workspace_root_logically(
        self,
        integration_client,
    ):
        from infra.database import get_db

        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name("logical-root-update")},
        )
        assert created.status_code == 200, created.text
        project = created.json()["project"]
        root = Path(project["path"]).parent / _unique_name("moved-root")
        root.mkdir()

        patched = integration_client.patch(
            f"/api/projects/{project['id']}?token={_TOKEN}",
            json={"path": str(root)},
        )

        assert patched.status_code == 200, patched.text
        assert patched.json()["project"]["path"] == str(root)
        db = get_db()
        try:
            stored = db.execute(
                "SELECT path FROM projects WHERE id = ?",
                (project["id"],),
            ).fetchone()["path"]
        finally:
            db.close()
        assert stored.startswith("workspace://")
        assert stored.endswith(f"/projects/{root.name}")

    def test_list_returns_accent_color(self, integration_client):
        name = _unique_name("list-accent")
        integration_client.post(
            f"/api/projects?token={_TOKEN}", json={"name": name, "accent_color": "#0d9488"}
        )
        lst = integration_client.get(f"/api/projects?token={_TOKEN}&status=all")
        assert lst.status_code == 200, lst.text
        row = next(p for p in lst.json()["projects"] if p["name"] == name)
        assert row["accent_color"] == "#0d9488"

    def test_invalid_accent_color_rejected(self, integration_client):
        bad = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique_name("bad"), "accent_color": "red; background: url(x)"},
        )
        assert bad.status_code == 400, bad.text

        ok = integration_client.post(
            f"/api/projects?token={_TOKEN}", json={"name": _unique_name("ok"), "accent_color": "#abc"}
        )
        assert ok.status_code == 200, ok.text
        pid = ok.json()["project"]["id"]
        badp = integration_client.patch(
            f"/api/projects/{pid}?token={_TOKEN}", json={"accent_color": "evil)"}
        )
        assert badp.status_code == 400, badp.text


@pytest.mark.integration
class TestProjectArchiveLifecycle:

    def test_archive_hides_from_active_then_restore_brings_back(self, integration_client):
        name = _unique_name("archive")
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}", json={"name": name, "status": "active"}
        )
        assert created.status_code == 200, created.text
        pid = created.json()["project"]["id"]

        def _active_ids():
            r = integration_client.get(f"/api/projects?token={_TOKEN}&status=active")
            assert r.status_code == 200, r.text
            return {p["id"] for p in r.json()["projects"]}

        assert pid in _active_ids()

        archived = integration_client.post(
            f"/api/projects/{pid}/transition?token={_TOKEN}",
            json={"to_status": "archived"},
        )
        assert archived.status_code == 200, archived.text
        assert pid not in _active_ids()

        all_r = integration_client.get(f"/api/projects?token={_TOKEN}&status=all")
        all_map = {p["id"]: p for p in all_r.json()["projects"]}
        assert all_map[pid]["status"] == "archived"

        restored = integration_client.post(
            f"/api/projects/{pid}/transition?token={_TOKEN}",
            json={"to_status": "active"},
        )
        assert restored.status_code == 200, restored.text
        assert pid in _active_ids()
