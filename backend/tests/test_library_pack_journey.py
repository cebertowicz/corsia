"""Exercise the blueprint journey through the tool and real owner routing."""

import hashlib
from pathlib import Path

from library.pack_digest import pack_digest
from tests.event_routing_helpers import mk_session


def test_blueprint_search_fetch_and_update_journey(isolated_db, monkeypatch):
    from infra import config
    from infra.database import get_db
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from library import index, items, producer
    from tools import skill_library

    workspace = Path(config.WORKSPACE)
    profile = workspace / PROFILE_RELATIVE_PATH
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(f"{USE_COMMUNITY_LABEL} Yes\n")
    project_root = workspace / "projects" / "blueprint-journey"
    project_root.mkdir(parents=True)
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id, name, path, created_at, updated_at) VALUES (?, ?, ?, 't', 't')",
                   ("blueprint-project", "Blueprint Journey", str(project_root)))
        db.commit()
    finally:
        db.close()
    owner = mk_session("blueprint-conductor", project_id="blueprint-project")
    worker = mk_session("blueprint-worker", project_id="blueprint-project")
    db = get_db()
    try:
        db.execute("UPDATE projects SET conductor_thread_id=? WHERE id=?", (owner, "blueprint-project"))
        db.commit()
    finally:
        db.close()

    files = {"manifest.yaml": b"schema: corsia.pack/1\nkind: blueprint\nname: demo\nversion: 1.0.0\n",
             "CHANGELOG.md": b"first\n", "context/PROJECT.md": b"project template\n"}
    row = {"kind": "pack", "pack_kind": "blueprint", "type": "corsia-blueprint",
           "name": "demo", "description": "Project blueprint", "version": "1.0.0",
           "source": {"repository": "example/library", "path": "packs/demo", "commit": "a" * 40},
           "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest()} for path, data in files.items()],
           "digest": pack_digest(files), "withdrawn": False, "withdrawn_reason": None}
    skill_content = b"---\nname: helper\ndescription: Project helper\n---\nUse it.\n"
    skill = {"name": "helper", "description": "Project helper", "version": "1.0.0", "license": "MIT",
             "source": {"repository": "example/library", "path": "skills/helper", "commit": "a" * 40},
             "files": [{"path": "SKILL.md", "sha256": hashlib.sha256(skill_content).hexdigest()}],
             "withdrawn": False, "withdrawn_reason": None}
    connector = {"kind": "pack", "pack_kind": "connector", "name": "connector"}
    catalog = {"skills": [row, skill, connector, {"kind": "future"}]}
    monkeypatch.setattr(index, "_fetch_index", lambda: catalog)
    monkeypatch.setattr(items, "_fetch_raw", lambda url: skill_content if "/skills/helper/" in url else files[url.split("/packs/demo/", 1)[1]])

    thread = {"project_id": "blueprint-project"}
    search = skill_library._execute("search", owner, thread, "project blueprint", None, False)
    assert {result["name"] for result in search["results"]} == {"demo", "helper"}
    assert next(result for result in search["results"] if result["name"] == "demo")["state"] == "not_fetched"
    assert skill_library._execute("search", owner, thread, "helper", None, False)["results"][0]["name"] == "helper"
    assert skill_library._execute("install", owner, thread, None, "helper", False)["outcome"] == "installed"
    assert skill_library._execute("fetch_pack", owner, thread, None, "connector", False)["error"] == "not_in_library"
    denied = skill_library._execute("fetch_pack", worker, thread, None, "demo", False)
    assert denied["error"] == "not_conductor"
    result = skill_library._execute("fetch_pack", owner, thread, None, "demo", False)
    assert result["outcome"] == "fetched"
    assert skill_library._execute("search", owner, thread, "demo", None, False)["results"][0]["state"] == "fetched"
    assert skill_library._execute("search", worker, thread, "demo", None, False)["results"][0]["state"] == "not_fetched"
    folder = project_root / ".threads" / owner / "packs" / "demo" / "1.0.0"
    assert Path(result["path"]) == folder
    assert {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()} == files
    db = get_db()
    try:
        registration = db.execute("""SELECT registrations.parent_thread_id, registrations.surfacing
            FROM artifact_identities AS identities JOIN artifact_registrations AS registrations
            ON registrations.artifact_id=identities.id
            WHERE identities.integration='library' AND identities.type='installed-pack' AND identities.external_id=?""",
            (f"{owner}/demo/1.0.0",)).fetchone()
        assert (registration["parent_thread_id"], registration["surfacing"]) == (owner, "quiet")
    finally:
        db.close()

    catalog["skills"] = [{**row, "version": "2.0.0", "digest": "a" * 64}, skill, connector]
    assert skill_library._execute("search", owner, thread, "demo", None, False)["results"][0]["state"] == "update_available"
    assert producer.produce_once() == 1
    db = get_db()
    try:
        notes = db.execute("SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", (owner,)).fetchall()
        assert len(notes) == 1
        assert "library.pack_changed" in notes[0]["summary"]
        assert "2.0.0" in notes[0]["summary"]
    finally:
        db.close()
    assert {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()} == files

    skill.update(withdrawn=True, withdrawn_reason="unsafe")
    assert producer.produce_once() == 1
    db = get_db()
    try:
        messages = db.execute("SELECT content FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (owner,)).fetchall()
        assert len(messages) == 1 and "library.item_withdrawn" in messages[0]["content"]
        assert "helper" in messages[0]["content"]
    finally:
        db.close()

    monkeypatch.setattr(index, "_fetch_index", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert skill_library._execute("search", owner, thread, "demo", None, False)["results"][0]["name"] == "demo"
    assert skill_library._execute("fetch_pack", owner, thread, None, "connector", False)["error"] == "not_in_library"
