"""The first-run About you doc is created where the reference says, under the real write policy."""
from pathlib import Path
import re

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session

REFERENCE = (
    Path(__file__).parents[1]
    / "prompts/system/v1/skills/conductor-bootstrap/references/about-you-doc.md"
)


def _section(number: int) -> str:
    text = REFERENCE.read_text(encoding="utf-8")
    return text.split(f"## {number}.", 1)[1].split("\n## ", 1)[0]


def _shared_paths(section: str) -> list[str]:
    return re.findall(r"`(shared/[^`\s]+)`", section)


@pytest.fixture
def first_run(isolated_db, tmp_path, monkeypatch):
    from core.workspace_scaffolding import scaffold_workspace
    from infra import config
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
    from infra.database import get_db
    from registry import service

    workspace = scaffold_workspace(tmp_path / "workspace").resolve()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", WorkspaceSelection(
        workspace_id="workspace-stable-id", root=workspace, name="Test workspace",
    ))
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **_kwargs: [workspace])
    db = get_db()
    agent = make_session(db, title="CORSIA", prefix="corsia")
    db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (agent,))
    set_scope_coordinator(WORKSPACE_SCOPE, agent, db=db)
    db.commit()
    return workspace, agent


def test_the_reference_section_1_writes_pass_the_shared_policy_in_a_new_workspace(first_run):
    from registry.durable_writes import write_text

    workspace, agent = first_run
    written = []
    for path in _shared_paths(_section(1)):
        target = workspace / path
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        result = write_text(
            target,
            f"{existing}# About you\n",
            thread_id=agent,
            project_id=None,
            workspace=workspace,
            description="About you",
            update_trigger="when the person's details change",
        )
        assert result is not None and result["ok"], path
        written.append(path)

    assert written == ["shared/about/INDEX.md", "shared/INDEX.md", "shared/about/about-you.md"]
    assert (workspace / written[-1]).is_file()
    assert "about-you.md" in (workspace / written[0]).read_text(encoding="utf-8")


def test_the_review_step_resolves_the_document_section_1_creates():
    created = _shared_paths(_section(1))[-1]
    assert _shared_paths(_section(3)) == [created]
