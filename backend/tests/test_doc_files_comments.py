
from __future__ import annotations

import os
import shutil
import sys
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytestmark = pytest.mark.integration

_TOKEN = OWNER_TOKEN


@pytest.fixture
def doc_file(integration_workspace):
    p = integration_workspace / f"notes-spec-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# Spec\n\nThe router decides where an inbound event goes.\n")
    yield p
    p.unlink(missing_ok=True)



def test_tree_lists_workspace_files(integration_client, doc_file):
    registered = integration_client.post(
        f"/api/files/register?token={_TOKEN}",
        json={"path": str(doc_file)},
    )
    assert registered.status_code == 200
    artifact_id = registered.json()["artifact"]["id"]

    r = integration_client.get(f"/api/files/tree?token={_TOKEN}")
    assert r.status_code == 200
    roots = r.json()["roots"]
    assert roots and roots[0]["kind"] == "workspace"
    rels = [n["rel"] for n in roots[0]["nodes"]]
    assert doc_file.name in rels
    node = next(node for node in roots[0]["nodes"] if node["rel"] == doc_file.name)
    assert node["artifact"]["id"] == artifact_id


def test_tree_prefers_logical_artifact_when_legacy_identity_has_same_path(
    integration_client,
    doc_file,
):
    from infra.database import get_db
    from registry import models, service

    logical_owner = _new_session(integration_client, "logical owner")
    legacy_owner = _new_session(integration_client, "legacy owner")
    logical = service.register_write_strict(logical_owner, str(doc_file))
    legacy = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=str(doc_file.resolve()),
        parent_thread_id=legacy_owner,
        title=doc_file.name,
        path=str(doc_file.resolve()),
    )
    assert legacy["id"] != logical["id"]

    db = get_db()
    try:
        from api.files_roots import artifact_index

        db.execute("PRAGMA reverse_unordered_selects = ON")
        indexed = artifact_index(db)[str(doc_file.resolve())]
    finally:
        db.execute("PRAGMA reverse_unordered_selects = OFF")
        db.close()

    assert indexed["id"] == logical["id"]
    assert indexed["external_id"] == logical["external_id"]


def test_content_reads_and_registers_on_open(integration_client, doc_file):
    r = integration_client.get(f"/api/files/content?token={_TOKEN}&path={doc_file}")
    assert r.status_code == 200
    body = r.json()
    assert "router decides" in body["text"]

    sid_r = integration_client.post(
        f"/api/threads?token={_TOKEN}", json={"title": "doc test"}
    )
    sid = sid_r.json()["thread_id"]
    r2 = integration_client.get(
        f"/api/files/content?token={_TOKEN}&path={doc_file}&thread_id={sid}"
    )
    assert r2.status_code == 200
    art = r2.json()["artifact"]
    assert art and art["parent_thread_id"] == sid
    assert art["type"] == "document"
    assert art["external_id"].startswith("workspace://")
    assert art["path"] == str(doc_file.resolve())

    reopened = integration_client.get(
        f"/api/files/content?token={_TOKEN}&path={doc_file}"
    )
    assert reopened.status_code == 200
    assert reopened.json()["artifact"]["id"] == art["id"]


def test_content_serves_context_dir_system_bootstrap_file(integration_client, integration_workspace):
    # Directory-as-truth (Phase 1b): static system files live in the workspace
    # context/ dir, dir-scanned as system bootstrap sources. They remain
    # READABLE via the content API but stay READ-ONLY via the doc-write API —
    # the system prompt must not be user-editable through the doc surface
    # (the Conductor owns system-bootloader updates). Only the out-of-tree
    # hardcoded-path special-case was removed, NOT the write protection.
    ctx_dir = integration_workspace / "context"
    ctx_dir.mkdir(exist_ok=True)
    system_file = ctx_dir / "CORSIA.md"
    original = system_file.read_text() if system_file.exists() else None
    system_file.write_text("# CORSIA\n\nSystem truth lives here.\n")
    try:
        r = integration_client.get(f"/api/files/content?token={_TOKEN}&path={system_file}")
        assert r.status_code == 200
        assert "System truth lives here" in r.json()["text"]

        # Write-protected: the doc-write API must refuse system bootstrap files
        # even in-tree — otherwise it's a system-prompt-injection vector.
        w = integration_client.put(
            f"/api/files/content?token={_TOKEN}",
            json={"path": str(system_file), "text": "# hijacked\n\nInjected.\n"},
        )
        assert w.status_code == 403
        assert "System truth lives here" in system_file.read_text()
    finally:
        if original is None:
            system_file.unlink(missing_ok=True)
        else:
            system_file.write_text(original)



@pytest.fixture
def working_doc(integration_workspace):
    d = integration_workspace / "working"
    d.mkdir(exist_ok=True)
    p = d / f"draft-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# Draft\n\nA working doc the operator pinned for review.\n")
    yield p
    p.unlink(missing_ok=True)


def _new_session(client, title):
    return client.post(
        f"/api/threads?token={_TOKEN}", json={"title": title}
    ).json()["thread_id"]


def _pane(client, sid):
    r = client.get(f"/api/threads/{sid}/artifacts?token={_TOKEN}")
    assert r.status_code == 200, r.text
    return r.json()


def _node_ids(pane):
    return {n["id"] for n in pane["nodes"] if n["kind"] == "artifact"}


def _pin_durable(working_doc, sid):
    from registry import models
    return models.register_artifact(
        integration="workspace", type="document", external_id=str(working_doc),
        parent_thread_id=sid, title=working_doc.name, path=str(working_doc),
        retention="durable",
    )


def test_open_keeps_pinned_working_doc_visible(integration_client, working_doc):
    sid = _new_session(integration_client, "pin test")

    art = _pin_durable(working_doc, sid)
    art_id = art["id"]
    assert art["retention"] == "durable"
    assert art_id in _node_ids(_pane(integration_client, sid))

    opened = integration_client.get(
        f"/api/files/content?token={_TOKEN}&path={working_doc}&thread_id={sid}"
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["artifact"]["retention"] == "durable"

    assert art_id in _node_ids(_pane(integration_client, sid))


def test_write_preserves_pinned_working_doc(integration_client, working_doc):
    sid = _new_session(integration_client, "write pin test")
    art_id = _pin_durable(working_doc, sid)["id"]

    saved = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={"path": str(working_doc), "text": "# Edited\n", "thread_id": sid},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["artifact"]["retention"] == "durable"
    assert art_id in _node_ids(_pane(integration_client, sid))


def test_open_unpinned_working_doc_defaults_ephemeral_but_visible(
    integration_client, working_doc,
):
    """The pane has no separate Working lane: an unpinned working/ doc
    defaults to ephemeral retention but still shows as a normal visible
    node (design: quiet artifacts, not ephemeral ones, are what disappear
    from the primary list)."""
    sid = _new_session(integration_client, "default ephemeral test")

    opened = integration_client.get(
        f"/api/files/content?token={_TOKEN}&path={working_doc}&thread_id={sid}"
    )
    assert opened.status_code == 200, opened.text
    art = opened.json()["artifact"]
    assert art["retention"] == "ephemeral"
    assert art["surfacing"] == "visible"
    assert art["id"] in _node_ids(_pane(integration_client, sid))


def test_content_rejects_out_of_scope_path(integration_client):
    r = integration_client.get(f"/api/files/content?token={_TOKEN}&path=/etc/hosts")
    assert r.status_code == 400


def test_write_error_status_parity(integration_client, integration_workspace, tmp_path, monkeypatch):
    from doceditor import document_save
    from registry.shared_write_errors import DurableWritePolicyError

    def write(path, text, **extra):
        return integration_client.put(
            f"/api/files/content?token={_TOKEN}",
            json={"path": str(path), "text": text, **extra},
        )

    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    assert write(outside, "outside\n").status_code == 400

    large = integration_workspace / "large.md"
    large.write_text("before\n")
    assert write(large, "x" * 1_000_001).status_code == 413

    existing = integration_workspace / "existing.md"
    existing.write_text("before\n")
    assert write(existing, "after\n", create=True).status_code == 409

    skill = integration_workspace / "skills" / "invalid" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Existing\n")
    invalid = write(skill, "not a skill\n")
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    assert detail["message"] == "Invalid SKILL.md"
    assert detail["line"].startswith(
        "Error: SKILL.md not saved — it would not load as a skill. The file is unchanged.\n"
    )
    assert str(skill) in detail["line"]
    assert all(error in detail["line"] for error in detail["errors"])
    assert "Fix: " in detail["line"]
    assert skill.read_text() == "# Existing\n"

    def refuse(*args, **kwargs):
        raise DurableWritePolicyError("policy denied")

    monkeypatch.setattr(document_save, "write_text", refuse)
    policy = write(existing, "after\n")
    assert policy.status_code == 403
    assert policy.json()["detail"] == "policy denied"


@pytest.fixture
def canonical_repo_project(integration_workspace, tmp_path_factory):
    from infra.database import get_db

    valid_repo = tmp_path_factory.mktemp("valid_repo")
    valid_file = valid_repo / "valid.py"
    valid_file.write_text("# canonical repo\n")
    unregistered_repo = tmp_path_factory.mktemp("unregistered_repo")
    unregistered_file = unregistered_repo / "unregistered.py"
    unregistered_file.write_text("# unregistered repo\n")
    original_repo = tmp_path_factory.mktemp("original_repo")
    replacement_repo = tmp_path_factory.mktemp("replacement_repo")
    replacement_file = replacement_repo / "replacement.py"
    replacement_file.write_text("# retargeted repo\n")
    legacy_repo = tmp_path_factory.mktemp("legacy_repo")

    name = f"Safehouse {uuid.uuid4().hex[:6]}"
    slug = name.lower().replace(" ", "-")
    proj_dir = integration_workspace / "projects" / slug
    source_dir = proj_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "valid").symlink_to(valid_repo, target_is_directory=True)
    (source_dir / "unregistered").symlink_to(
        unregistered_repo,
        target_is_directory=True,
    )
    (source_dir / "retargeted").symlink_to(
        replacement_repo,
        target_is_directory=True,
    )
    (proj_dir / "project_source").symlink_to(
        legacy_repo,
        target_is_directory=True,
    )

    from infra import config
    from infra.workspace_paths import WorkspaceReference

    pid = uuid.uuid4().hex
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id, name, path, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (
                pid,
                name,
                WorkspaceReference.from_path(
                    proj_dir, config.WORKSPACE_SELECTION
                ).serialize(),
            ),
        )
        db.execute(
            "INSERT INTO project_repo_policies "
            "(project_id, repo_name, repo_path, pinned_branch, "
            "protect_base_checkout) VALUES (?, ?, ?, NULL, 0)",
            (pid, "valid", str(valid_repo.resolve())),
        )
        db.execute(
            "INSERT INTO project_repo_policies "
            "(project_id, repo_name, repo_path, pinned_branch, "
            "protect_base_checkout) VALUES (?, ?, ?, NULL, 0)",
            (pid, "retargeted", str(original_repo.resolve())),
        )
        db.commit()
    finally:
        db.close()

    yield {
        "project_id": pid,
        "valid_file": valid_file,
        "unregistered_file": unregistered_file,
        "replacement_file": replacement_file,
    }

    db = get_db()
    try:
        db.execute("DELETE FROM project_repo_policies WHERE project_id=?", (pid,))
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
        db.commit()
    finally:
        db.close()
    shutil.rmtree(proj_dir, ignore_errors=True)


def test_files_api_uses_only_valid_canonical_repo_bindings(
    integration_client,
    canonical_repo_project,
):
    pid = canonical_repo_project["project_id"]
    valid_file = canonical_repo_project["valid_file"]
    unregistered_file = canonical_repo_project["unregistered_file"]
    replacement_file = canonical_repo_project["replacement_file"]

    tr = integration_client.get(f"/api/files/tree?token={_TOKEN}&project_id={pid}")
    assert tr.status_code == 200
    repo_roots = [r for r in tr.json()["roots"] if r["kind"] == "repo"]
    assert [(root["label"], root["path"]) for root in repo_roots] == [
        ("valid", str(valid_file.parent.resolve()))
    ]

    cr = integration_client.get(
        f"/api/files/content?token={_TOKEN}&path={valid_file}&project_id={pid}"
    )
    assert cr.status_code == 200, cr.text
    assert "canonical repo" in cr.json()["text"]

    for denied in (unregistered_file, replacement_file):
        response = integration_client.get(
            f"/api/files/content?token={_TOKEN}&path={denied}&project_id={pid}"
        )
        assert response.status_code == 400, response.text


def test_write_rejects_session_project_scope_mismatch(
    integration_client,
    canonical_repo_project,
):
    sid = _new_session(integration_client, "scope mismatch")
    pid = canonical_repo_project["project_id"]
    valid_file = canonical_repo_project["valid_file"]
    original = valid_file.read_text(encoding="utf-8")

    response = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={
            "path": str(valid_file),
            "text": "cross-project write\n",
            "project_id": pid,
            "thread_id": sid,
        },
    )

    assert response.status_code == 403, response.text
    assert valid_file.read_text(encoding="utf-8") == original


def test_register_materializes_ui_artifact(integration_client, doc_file):
    r = integration_client.post(
        f"/api/files/register?token={_TOKEN}",
        json={"path": str(doc_file)},
    )
    assert r.status_code == 200
    art = r.json()["artifact"]
    assert art["integration"] == "workspace"
    assert art["parent_thread_id"] == "ui"
    assert art["external_id"].startswith("workspace://")
    assert art["path"] == str(doc_file.resolve())

def test_content_write_roundtrip_and_create(integration_client, integration_workspace, doc_file):
    r = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={"path": str(doc_file), "text": "# Spec v2\n\nRewritten.\n"},
    )
    assert r.status_code == 200 and r.json()["saved"]
    r2 = integration_client.get(f"/api/files/content?token={_TOKEN}&path={doc_file}")
    assert "Rewritten" in r2.json()["text"]

    new = integration_workspace / "working" / f"note-{uuid.uuid4().hex[:6]}.md"
    r3 = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={"path": str(new), "text": "fresh\n", "create": True},
    )
    assert r3.status_code == 200
    assert new.read_text() == "fresh\n"
    new.unlink()

    ghost = integration_workspace / "working" / "ghost.md"
    r4 = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={"path": str(ghost), "text": "x"},
    )
    assert r4.status_code == 404


def test_shared_document_write_is_atomic_and_reuses_identity(
    integration_client,
    integration_workspace,
):
    from infra.database import get_db

    category = integration_workspace / "shared" / f"planning-{uuid.uuid4().hex[:8]}"
    category.mkdir(parents=True)
    (category / "INDEX.md").write_text("# Planning\n", encoding="utf-8")
    target = category / "roadmap.md"
    missing_description = category / "undocumented.md"
    sid = _new_session(integration_client, "shared document writer")
    try:
        created = integration_client.put(
            f"/api/files/content?token={_TOKEN}",
            json={
                "path": str(target),
                "text": "first\n",
                "thread_id": sid,
                "create": True,
                "description": "Current delivery roadmap",
            },
        )
        assert created.status_code == 200, created.text
        first = created.json()
        assert first["registry_managed"] is True
        assert first["artifact"]["external_id"].startswith("workspace://")
        assert first["artifact"]["path"] == str(target.resolve())

        edited = integration_client.put(
            f"/api/files/content?token={_TOKEN}",
            json={
                "path": str(target),
                "text": "second\n",
                "thread_id": sid,
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["artifact"]["id"] == first["artifact"]["id"]
        assert target.read_text(encoding="utf-8") == "second\n"

        rejected = integration_client.put(
            f"/api/files/content?token={_TOKEN}",
            json={
                "path": str(missing_description),
                "text": "missing\n",
                "thread_id": sid,
                "create": True,
            },
        )
        assert rejected.status_code == 403, rejected.text
        assert not missing_description.exists()

        db = get_db()
        try:
            assert db.execute(
                "SELECT COUNT(*) FROM durable_write_intents"
            ).fetchone()[0] == 0
        finally:
            db.close()
    finally:
        shutil.rmtree(category, ignore_errors=True)


def test_direct_user_shared_write_uses_atomic_seam(
    integration_client,
    integration_workspace,
):
    category = integration_workspace / "shared" / f"user-{uuid.uuid4().hex[:8]}"
    category.mkdir(parents=True)
    (category / "INDEX.md").write_text("# User documents\n", encoding="utf-8")
    target = category / "decision.md"
    try:
        response = integration_client.put(
            f"/api/files/content?token={_TOKEN}",
            json={
                "path": str(target),
                "text": "approved\n",
                "create": True,
                "description": "User-authored decision",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["registry_managed"] is True
        assert target.read_text(encoding="utf-8") == "approved\n"
    finally:
        shutil.rmtree(category, ignore_errors=True)
