"""Whole-project installs by the lead agent, and the library on/off by who wrote the item."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.project_ref_helpers import project_ref, select_workspace, write_project_context

SKILL_MD = b"---\nname: shared-skill\ndescription: Do the shared job.\nlicense: Apache-2.0\nmetadata:\n  version: \"1.0.0\"\n---\n# Shared\nDo it.\n"


def _item(name: str = "shared-skill", *, author: str | None = "corsia", repository: str = "cebertowicz/corsia-library") -> dict:
    row = {
        "name": name,
        "description": "Do the shared job.",
        "version": "1.0.0",
        "license": "Apache-2.0",
        "source": {"repository": repository, "commit": "a" * 40, "path": f"skills/{name}"},
        "files": [{"path": "SKILL.md", "sha256": "sha256:" + hashlib.sha256(SKILL_MD).hexdigest()}],
        "withdrawn": False,
        "withdrawn_reason": None,
    }
    if author is not None:
        row["author"] = author
    return row


def _answer_community(workspace: Path, answer: str) -> None:
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL

    profile = workspace / PROFILE_RELATIVE_PATH
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(f"{USE_COMMUNITY_LABEL} {answer}\n", encoding="utf-8")


def test_only_items_the_library_records_as_corsias_count_as_corsias():
    from library.items import corsia_authored

    assert corsia_authored(_item())
    assert not corsia_authored(_item(author=None))
    assert not corsia_authored(_item(author="someone"))
    # An outside source can't claim CORSIA wrote it.
    assert not corsia_authored(_item(repository="openai/skills"))


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A library holding one CORSIA item and one item another user shared."""
    from library import items, producer
    from tools import skill_library

    index = (_item("corsia-skill"), _item("shared-by-someone", author=None))
    fetched: list[str] = []
    monkeypatch.setattr(skill_library, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=index, index_age="just now", refreshed=False))
    monkeypatch.setattr(skill_library, "_installed_library_origins", lambda *_args: {})
    monkeypatch.setattr(skill_library, "_persistent_owner", lambda thread_id, _db: thread_id)
    monkeypatch.setattr(skill_library, "_emit_action", lambda *_args: None)
    monkeypatch.setattr(items, "fetch_bundle", lambda item: fetched.append(item["name"]) or {"SKILL.md": SKILL_MD})
    monkeypatch.setattr(producer, "request_refresh", lambda: None)
    return SimpleNamespace(workspace=tmp_path, fetched=fetched)


def test_after_a_no_search_shows_only_corsias_items_and_says_why(isolated_db, library):
    from tools import skill_library

    _answer_community(library.workspace, "No")
    result = skill_library._execute("search", "thread", {}, "shared job", None, False)

    assert [row["name"] for row in result["results"]] == ["corsia-skill"]
    assert "Only CORSIA's own items are shown" in result["note"]


def test_after_a_yes_search_shows_everything_with_no_note(isolated_db, library):
    from tools import skill_library

    _answer_community(library.workspace, "Yes")
    result = skill_library._execute("search", "thread", {}, "shared job", None, False)

    assert sorted(row["name"] for row in result["results"]) == ["corsia-skill", "shared-by-someone"]
    assert "note" not in result


@pytest.mark.parametrize("action", ["install", "update", "fetch_pack"])
def test_after_a_no_an_item_another_user_shared_is_refused_before_any_fetch(isolated_db, library, monkeypatch, action):
    from library.items import source_ref
    from tools import skill_library

    _answer_community(library.workspace, "No")
    # update reads the local copy first; give it one, so the refusal comes from the author rule.
    copy = SimpleNamespace(name="shared-by-someone", path=str(library.workspace / "x" / "SKILL.md"), scope="thread",
                           corsia={"origin": {"source": source_ref(_item("shared-by-someone", author=None))}})
    monkeypatch.setattr(skill_library, "_find_local", lambda *_args, **_kwargs: copy)

    result = skill_library._execute(action, "thread", {}, None, "shared-by-someone", False)

    assert result["error"] == "community_off"
    assert "shared-by-someone was shared by someone other than CORSIA" in result["message"]
    assert "CORSIA's own library items still work" in result["message"]
    assert library.fetched == []


def test_after_a_no_corsias_own_item_still_installs(isolated_db, library, monkeypatch):
    from tools import skill_library

    _answer_community(library.workspace, "No")
    writes = []
    monkeypatch.setattr(skill_library, "_effective", lambda *_args: ([], []))
    monkeypatch.setattr(skill_library, "_ensure_registered", lambda *_args: None)
    import infra.skill_install as skill_install
    monkeypatch.setattr(skill_install, "install_skill_bundle", lambda *args, **kwargs: writes.append(kwargs) or {"outcome": "installed"})
    monkeypatch.setattr(skill_library, "_path", lambda *_args: "/copy/SKILL.md")

    result = skill_library._execute("install", "thread", {}, None, "corsia-skill", False)

    assert result["outcome"] == "installed"
    assert library.fetched == ["corsia-skill"]
    assert writes[0]["scope"] == "thread"


def test_change_notices_for_corsias_items_still_arrive_after_a_no(monkeypatch):
    from library import producer

    items = {"corsia-skill": _item("corsia-skill"), "shared-by-someone": _item("shared-by-someone", author=None)}
    assert producer._offered({"name": "corsia-skill"}, items, False)
    assert not producer._offered({"name": "shared-by-someone"}, items, False)
    assert producer._offered({"name": "shared-by-someone"}, items, True)


@pytest.mark.parametrize(("action", "thread", "code"), [
    ("search", {"project_id": "p"}, "invalid_scope"),
    ("fetch_pack", {"project_id": "p"}, "invalid_scope"),
    ("install", {}, "no_project"),
])
def test_project_scope_refusals_name_the_way_forward(isolated_db, library, action, thread, code):
    from tools import skill_library

    _answer_community(library.workspace, "Yes")
    result = skill_library._execute(action, "thread", thread, "skill", "corsia-skill", False, None, "project")

    assert result["error"] == code


@pytest.fixture
def project(isolated_db, monkeypatch, tmp_path):
    """A real project with a lead agent and a second standing agent."""
    from infra import config
    from infra.conductor import set_scope_coordinator
    from infra.database import write_transaction
    import uuid

    workspace = Path(config.WORKSPACE).resolve()
    select_workspace(monkeypatch, workspace)
    slug = f"lib-{uuid.uuid4().hex[:8]}"
    root = write_project_context(workspace, slug)
    project_id = f"project-{slug}"
    with write_transaction(None, scope="library_project_scope_seed") as db:
        db.execute("INSERT INTO projects (id, name, path, created_at, updated_at) VALUES (?,?,?,'t','t')",
                   (project_id, slug, project_ref(slug)))
        for thread_id in ("lead", "member"):
            db.execute("INSERT INTO threads (id, user_id, title, model, project_id, created_at, updated_at) "
                       "VALUES (?, 'user-1', ?, 'claude-code/sonnet', ?, 't', 't')", (thread_id, thread_id, project_id))
    from infra.database import get_db

    db = get_db()
    try:
        set_scope_coordinator(project_id, "lead", db=db)
        db.commit()
    finally:
        db.close()
    return SimpleNamespace(workspace=workspace, root=root, id=project_id)


def _tool_for(monkeypatch, workspace: Path, fetched: list[str]):
    from library import items, producer
    from tools import skill_library

    monkeypatch.setattr(skill_library, "_workspace", lambda: workspace)
    monkeypatch.setattr(skill_library, "_index", lambda: SimpleNamespace(items=(_item(),), index_age="just now", refreshed=False))
    monkeypatch.setattr(skill_library, "_emit_action", lambda *_args: None)

    def fetch_bundle(item: dict) -> dict[str, bytes]:
        fetched.append(item["name"])
        return {"SKILL.md": SKILL_MD}

    monkeypatch.setattr(items, "fetch_bundle", fetch_bundle)
    monkeypatch.setattr(producer, "request_refresh", lambda: None)
    _answer_community(workspace, "No")
    return skill_library


def test_only_the_lead_agent_changes_the_project_copy(project, monkeypatch):
    fetched: list[str] = []
    skill_library = _tool_for(monkeypatch, project.workspace, fetched)

    result = skill_library._execute("install", "member", {"project_id": project.id}, None, "shared-skill", False, None, "project")

    assert result["error"] == "not_lead_agent"
    assert "propose it to the lead agent" in result["message"]
    assert fetched == []
    assert not (project.root / "skills" / "shared-skill").exists()


def test_lead_agent_installs_once_for_every_agent_then_removes_it(project, monkeypatch):
    from core.skill_resolution import resolve_effective_skills
    from infra.database import get_db

    fetched: list[str] = []
    skill_library = _tool_for(monkeypatch, project.workspace, fetched)

    installed = skill_library._execute("install", "lead", {"project_id": project.id}, None, "shared-skill", False, None, "project")

    assert installed["outcome"] == "installed", installed
    assert installed["scope"] == "project"
    assert (project.root / "skills" / "shared-skill" / "SKILL.md").is_file()
    db = get_db()
    try:
        seen = resolve_effective_skills(thread_id="member", project_id=project.id, workspace=project.workspace, db=db)
        assert [(row.name, row.scope) for row in seen.included if row.name == "shared-skill"] == [("shared-skill", "project")]
        registered = db.execute(
            "SELECT identities.external_id, registrations.parent_thread_id FROM artifact_identities AS identities "
            "JOIN artifact_registrations AS registrations ON registrations.artifact_id=identities.id "
            "WHERE identities.integration='library' AND identities.type='installed-skill'"
        ).fetchall()
    finally:
        db.close()
    assert [(row[0], row[1]) for row in registered] == [(f"project/{project.id}/shared-skill", "lead")]

    member_install = skill_library._execute("install", "member", {"project_id": project.id}, None, "shared-skill", False)
    assert member_install["error"] == "already_installed"
    assert "project scope" in member_install["message"]

    member_remove = skill_library._execute("remove", "member", {"project_id": project.id}, None, "shared-skill", False)
    assert member_remove["error"] == "not_installed"
    assert "pass scope project" in member_remove["message"]

    up_to_date = skill_library._execute("update", "lead", {"project_id": project.id}, None, "shared-skill", False, None, "project")
    assert up_to_date["outcome"] == "up_to_date"

    removed = skill_library._execute("remove", "lead", {"project_id": project.id}, None, "shared-skill", False, None, "project")
    assert removed == {"outcome": "removed", "name": "shared-skill"}
    assert not (project.root / "skills" / "shared-skill").exists()
