
from __future__ import annotations

import os
import sys
import threading
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_A = OWNER_TOKEN   # user A (the operator)
_B = OTHER_TOKEN   # user B (the second seeded account)



def _session(client, token: str, title: str = "t") -> str:
    return client.post(f"/api/threads?token={token}", json={"title": title}).json()["thread_id"]


@pytest.fixture
def doc_file(integration_workspace):
    p = integration_workspace / f"authz-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# Doc\n\nUser A's document body.\n")
    yield p
    p.unlink(missing_ok=True)


def _register_for(client, token: str, sid: str, doc_file) -> str:
    from registry import models
    art = models.register_artifact(
        integration="workspace", type="document", external_id=str(doc_file),
        parent_thread_id=sid, title=doc_file.name, path=str(doc_file),
    )
    return art["id"]



@pytest.mark.integration
class TestFilesOwnership:
    def test_B_cannot_open_into_As_thread(self, integration_client, doc_file):
        a_sid = _session(integration_client, _A)
        r = integration_client.get(
            f"/api/files/content?token={_B}&path={doc_file}&thread_id={a_sid}")
        assert r.status_code == 404

    def test_B_cannot_write_into_As_thread(self, integration_client, doc_file):
        a_sid = _session(integration_client, _A)
        r = integration_client.put(
            f"/api/files/content?token={_B}",
            json={"path": str(doc_file), "text": "hijacked", "thread_id": a_sid})
        assert r.status_code == 404
        assert "User A's document body" in doc_file.read_text()

    def test_B_cannot_register_into_As_thread(self, integration_client, doc_file):
        a_sid = _session(integration_client, _A)
        r = integration_client.post(
            f"/api/files/register?token={_B}",
            json={"path": str(doc_file), "thread_id": a_sid})
        assert r.status_code == 404

    def test_A_owns_own_thread_unchanged(self, integration_client, doc_file):
        a_sid = _session(integration_client, _A)
        r = integration_client.get(
            f"/api/files/content?token={_A}&path={doc_file}&thread_id={a_sid}")
        assert r.status_code == 200
        assert r.json()["artifact"]["parent_thread_id"] == a_sid



@pytest.mark.integration
class TestCommentsOwnership:
    def _a_owned_artifact(self, client, doc_file) -> tuple[str, str]:
        a_sid = _session(client, _A)
        aid = _register_for(client, _A, a_sid, doc_file)
        return a_sid, aid

    def test_B_cannot_list_As_comments(self, integration_client, doc_file):
        _, aid = self._a_owned_artifact(integration_client, doc_file)
        assert integration_client.get(
            f"/api/artifacts/{aid}/comments?token={_B}").status_code == 404

    def test_B_cannot_post_to_As_artifact(self, integration_client, doc_file):
        _, aid = self._a_owned_artifact(integration_client, doc_file)
        r = integration_client.post(
            f"/api/artifacts/{aid}/comments?token={_B}", json={"body": "intrusion"})
        assert r.status_code == 404

    def test_B_cannot_persist_positions_on_As_artifact(self, integration_client, doc_file):
        _, aid = self._a_owned_artifact(integration_client, doc_file)
        r = integration_client.post(
            f"/api/artifacts/{aid}/comments/positions?token={_B}",
            json={"updates": [{"id": "x", "from_off": 1, "to_off": 2}]})
        assert r.status_code == 404

    def test_B_cannot_patch_As_comment(self, integration_client, doc_file):
        _, aid = self._a_owned_artifact(integration_client, doc_file)
        cid = integration_client.post(
            f"/api/artifacts/{aid}/comments?token={_A}", json={"body": "A's note"}
        ).json()["comment"]["id"]
        r = integration_client.patch(
            f"/api/comments/{cid}?token={_B}", json={"resolved": True})
        assert r.status_code == 404
        assert integration_client.patch(
            f"/api/comments/{cid}?token={_A}", json={"resolved": True}
        ).status_code == 200

    def test_A_full_flow_unchanged(self, integration_client, doc_file):
        _, aid = self._a_owned_artifact(integration_client, doc_file)
        assert integration_client.get(
            f"/api/artifacts/{aid}/comments?token={_A}").status_code == 200
        assert integration_client.post(
            f"/api/artifacts/{aid}/comments?token={_A}", json={"body": "ok"}
        ).status_code == 200



@pytest.mark.integration
class TestArtifactPatchOwnership:
    def test_B_cannot_patch_As_artifact_description(self, integration_client, doc_file):
        a_sid = _session(integration_client, _A)
        aid = _register_for(integration_client, _A, a_sid, doc_file)
        r = integration_client.patch(
            f"/api/artifacts/{aid}?token={_B}", json={"description": "B was here"})
        assert r.status_code == 404
        ok = integration_client.patch(
            f"/api/artifacts/{aid}?token={_A}", json={"description": "A's desc"})
        assert ok.status_code == 200
        assert ok.json()["artifact"]["description"] == "A's desc"

    def test_B_cannot_patch_via_register_rehome(self, integration_client, doc_file):
        from registry import models
        a_sid = _session(integration_client, _A)
        b_sid = _session(integration_client, _B)
        first = _register_for(integration_client, _A, a_sid, doc_file)
        again = models.register_artifact(
            integration="workspace", type="document", external_id=str(doc_file),
            parent_thread_id=b_sid, title=doc_file.name, path=str(doc_file))
        assert again["id"] == first
        assert again["parent_thread_id"] == a_sid, "register must not re-home"



@pytest.mark.integration
class TestProjectScopeConfines:
    def test_project_scope_rejects_bare_workspace_path(self, integration_client, doc_file):
        p = integration_client.post(
            f"/api/projects?token={_A}", json={"name": f"confine-{uuid.uuid4().hex[:6]}"}
        ).json()["project"]

        scoped = integration_client.get(
            f"/api/files/content?token={_A}&path={doc_file}&project_id={p['id']}")
        assert scoped.status_code == 400, "project scope must not admit a bare-workspace path"

        unscoped = integration_client.get(
            f"/api/files/content?token={_A}&path={doc_file}")
        assert unscoped.status_code == 200, "the unscoped workspace read is unchanged"



@pytest.mark.integration
class TestRegisterTOCTOU:
    def test_concurrent_first_register_no_integrity_error(self, integration_client):
        from infra.database import get_db
        from registry import models
        from tests.registry_helpers import make_session

        db = get_db()
        try:
            sid_a = make_session(db, prefix="toctA", user_id="steve")
            sid_b = make_session(db, prefix="toctB", user_id="leksi")
        finally:
            db.close()

        ext = f"toctou-{uuid.uuid4().hex[:10]}"
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}
        errors: list[Exception] = []

        def _go(name: str, parent: str):
            try:
                barrier.wait(timeout=5)  # release both threads together
                results[name] = models.register_artifact(
                    integration="workspace", type="document", external_id=ext,
                    parent_thread_id=parent, title="race.md")
            except Exception as e:  # noqa: BLE001 — surface any IntegrityError loudly
                errors.append(e)

        t1 = threading.Thread(target=_go, args=("a", sid_a))
        t2 = threading.Thread(target=_go, args=("b", sid_b))
        t1.start(); t2.start(); t1.join(); t2.join()

        assert not errors, f"concurrent register raised: {errors!r}"
        assert set(results) == {"a", "b"}

        db = get_db()
        try:
            rows = db.execute(
                """SELECT i.id, r.parent_thread_id
                   FROM artifact_identities i
                   JOIN artifact_registrations r ON r.artifact_id=i.id
                   WHERE i.external_id=?
                     AND r.relationship_state IN ('owner-pinned', 'owner-active')""",
                (ext,)).fetchall()
        finally:
            db.close()
        assert len(rows) == 1, "the identity index must hold — one row only"
        owner = rows[0]["parent_thread_id"]
        assert owner in {sid_a, sid_b}
        assert results["a"]["id"] == results["b"]["id"] == rows[0]["id"]
        assert results["a"]["parent_thread_id"] == results["b"]["parent_thread_id"] == owner
