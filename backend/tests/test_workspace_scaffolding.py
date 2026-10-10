"""Canonical workspace and project scaffold contracts."""

from __future__ import annotations

from pathlib import Path

import pytest


WORKSPACE_INDEX = (
    "# Workspace\n\n"
    "- `context/` — workspace context documents.\n"
    "- `skills/` — workspace skills.\n"
    "- `shared/` — categorized durable shared material.\n"
    "- `projects/` — project workspaces.\n"
    "- `.threads/` — thread-local context, skills, and scratch.\n"
)
PROJECT_INDEX = (
    "# Home Assistant\n\n"
    "- `context/` — project context documents.\n"
    "- `skills/` — project skills.\n"
    "- `shared/` — categorized durable shared material.\n"
    "- `.threads/` — thread-local context, skills, and scratch.\n"
    "- `source/` — project repositories.\n"
)
WORKSPACE_AGENTS = (
    "# Offline Codex Connector\n\n"
    "Generated pointer shell for direct offline use. CORSIA-managed sessions "
    "receive context and tools through CORSIA.\n\n"
    "Before acting, enumerate and read every top-level Markdown file in "
    "`context/` in filename order.\n\n"
    "If a required source cannot be read, stop and report it instead of "
    "proceeding with partial workspace policy.\n"
)
WORKSPACE_CLAUDE = (
    "# Offline Claude Code Connector\n\n"
    "Generated pointer shell for direct offline use. CORSIA-managed sessions "
    "receive context and tools through CORSIA.\n\n"
    "Before acting, enumerate and read every top-level Markdown file in "
    "`context/` in filename order.\n\n"
    "If a required source cannot be read, stop and report it instead of "
    "proceeding with partial workspace policy.\n"
)
PROJECT_CLAUDE = (
    "# Offline Claude Code Connector\n\n"
    "Generated pointer shell for direct offline use. CORSIA-managed sessions "
    "receive context and tools through CORSIA.\n\n"
    "Before acting, enumerate every top-level Markdown file in `context/` and "
    "read them in canonical authority order:\n\n"
    "1. `context/PROJECT.md`\n"
    "2. vision and intent files, in filename order\n"
    "3. safety and invariant files, in filename order\n"
    "4. all other project context files, in filename order\n"
    "5. `context/COORDINATION-MODEL.md`\n\n"
    "`context/PROJECT.md` and `context/COORDINATION-MODEL.md` are required. "
    "If either is missing, or any discovered source cannot be read, stop and "
    "report it instead of proceeding with partial project policy.\n"
)
PROJECT_AGENTS = (
    "# Offline Codex Connector\n\n"
    "Generated pointer shell for direct offline use. CORSIA-managed sessions "
    "receive context and tools through CORSIA.\n\n"
    "Before acting, enumerate every top-level Markdown file in `context/` and "
    "read them in canonical authority order:\n\n"
    "1. `context/PROJECT.md`\n"
    "2. vision and intent files, in filename order\n"
    "3. safety and invariant files, in filename order\n"
    "4. all other project context files, in filename order\n"
    "5. `context/COORDINATION-MODEL.md`\n\n"
    "`context/PROJECT.md` and `context/COORDINATION-MODEL.md` are required. "
    "If either is missing, or any discovered source cannot be read, stop and "
    "report it instead of proceeding with partial project policy.\n"
)


def _names(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


def test_workspace_scaffold_creates_exact_canonical_layout_and_bytes(tmp_path):
    from core.workspace_scaffolding import scaffold_workspace

    workspace = tmp_path / "workspace"

    assert scaffold_workspace(workspace) == workspace

    assert _names(workspace) == {
        "INDEX.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".gitignore",
        "context",
        "skills",
        "shared",
        "projects",
        ".threads",
    }
    assert _names(workspace / "shared") == {"INDEX.md"}
    assert (workspace / "INDEX.md").read_text(encoding="utf-8") == WORKSPACE_INDEX
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == WORKSPACE_AGENTS
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8") == WORKSPACE_CLAUDE
    assert (workspace / ".gitignore").read_text(encoding="utf-8") == (
        "# Thread-local scratch is ephemeral.\n"
        ".threads/*/working/\n"
        "projects/*/.threads/*/working/\n"
        "\n# Project source checkouts are separate repositories.\n"
        "projects/*/source/\n"
        "\n# Finder metadata is not workspace truth.\n"
        ".DS_Store\n"
    )
    assert not (workspace / "working").exists()
    assert not (workspace / "PROJECT.md").exists()
    assert not (workspace / "BRIEF.md").exists()


def test_project_scaffold_creates_exact_canonical_layout_and_bytes(tmp_path):
    from core.workspace_scaffolding import scaffold_project

    project = tmp_path / "projects" / "home-assistant"

    assert (
        scaffold_project(project, name="Home Assistant", description="Automation")
        == project
    )

    assert _names(project) == {
        "INDEX.md",
        "AGENTS.md",
        "CLAUDE.md",
        "context",
        "skills",
        "shared",
        ".threads",
        "source",
    }
    assert _names(project / "context") == {"PROJECT.md", "COORDINATION-MODEL.md"}
    assert _names(project / "shared") == {"INDEX.md"}
    assert (project / "INDEX.md").read_text(encoding="utf-8") == PROJECT_INDEX
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == PROJECT_AGENTS
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == PROJECT_CLAUDE
    assert (project / "context" / "PROJECT.md").read_text(encoding="utf-8") == (
        "# Home Assistant\n\n"
        "## Purpose\n"
        "Automation\n\n"
        "## Goal hierarchy\n\n"
        "### Long-horizon objectives\n"
        "- _(Directional outcome and the trade-off when goals compete.)_\n\n"
        "### Current strategic objectives\n"
        "- _(Current objective, success evidence, and review horizon—not live task state.)_\n\n"
        "## User position and mandate\n"
        "- **Relationship:** _(Owner, operator, contributor, advisor, approver, or another actual position.)_\n"
        "- **Accountable for:** _(Outcomes and obligations.)_\n"
        "- **May decide:** _(Substantive decision rights.)_\n"
        "- **May execute:** _(Actions the user may perform or direct.)_\n"
        "- **May commit or represent:** _(What the user may promise, publish, approve, or communicate for others.)_\n"
        "- **Requires another authority for:** _(Approval chain and boundaries.)_\n\n"
        "## Constraints and non-goals\n"
        "- _(Hard constraint or outcome deliberately not pursued.)_\n\n"
        "## Durable risks, assumptions, and blind spots\n"
        "- _(Durable item, observable signal, and expected response. Keep live risks in work state.)_\n\n"
        "## Stable project topology\n"
        "- _(Resource or system, location, and purpose.)_\n\n"
        "## Canonical sources\n"
        "- _(Question, authoritative source, and freshness rule.)_\n"
    )
    assert (project / "context" / "COORDINATION-MODEL.md").read_text(
        encoding="utf-8"
    ) == (
        "# Home Assistant — coordination model\n\n"
        "This is the shared project operating and context-delivery contract. "
        "It is not live status, an agent-specific manual, or the conductor's complete project graph.\n\n"
        "## Leverage objective\n"
        "_(What coordination burden should be removed from the user, and what better outcome should result?)_\n\n"
        "## Operating model\n"
        "- **User:** _(Project-wide substantive authority and responsibility.)_\n"
        "- **Conductor:** Designs and maintains coordination within the user's established mandate.\n"
        "- **Persistent agents:** Own durable responsibilities and receive additive responsibility-specific context.\n"
        "- **Temporary delegates:** Receive bounded task context and return evidence to their parent.\n\n"
        "## Context layers\n"
        "- **Project bootloader:** Stable truth every project thread needs.\n"
        "- **Persistent-agent bootloader:** Responsibility-specific goals, authority, risks, sources, and workflows.\n"
        "- **Thread plan or work state:** Live owned work.\n"
        "- **Task brief:** Temporary context for one bounded result.\n\n"
        "Your own plan's bounded status is delivered with context; parent and adjacent plans are not. Retrieve exact live detail through plan_read.\n\n"
        "## Shared work and context routes\n"
        "- _(Work or decision class, authoritative context, trigger or freshness rule, and verification or failure behavior.)_\n\n"
        "## Shared ownership and failure behavior\n"
        "- _(Project-wide ownership seam, approval boundary, or loud failure rule.)_\n\n"
        "Integrate authoritative sources when practical. For an unintegrated source, record its access trigger, owner, freshness rule, and failure behavior.\n\n"
        "Missing, stale, unreadable, or contradictory required context fails visibly; never substitute a convenient older source.\n\n"
        "## Conductor stewardship\n"
        "Review outcomes and context gaps, make the smallest durable improvement, verify delivery, and retire superseded policy. "
        "Do not rewrite the user's goals, mandate, commitments, or accepted risks without user direction.\n"
    )
    assert not (project / "working").exists()
    assert not (project / "PROJECT.md").exists()
    assert not (project / "BRIEF.md").exists()


def test_project_connectors_cover_later_context_files_without_regeneration(tmp_path):
    from core.project_context import project_context_filenames
    from core.workspace_scaffolding import scaffold_project

    project = scaffold_project(
        tmp_path / "projects" / "corsia",
        name="CORSIA",
        description="Agent orchestration",
    )
    (project / "context" / "VISION-CORE.md").write_text("vision", encoding="utf-8")
    (project / "context" / "SAFETY-INVARIANTS.md").write_text(
        "safety", encoding="utf-8"
    )

    assert project_context_filenames(project) == [
        "context/PROJECT.md",
        "context/VISION-CORE.md",
        "context/SAFETY-INVARIANTS.md",
        "context/COORDINATION-MODEL.md",
    ]
    for connector in ("AGENTS.md", "CLAUDE.md"):
        content = (project / connector).read_text(encoding="utf-8")
        assert "enumerate every top-level Markdown file in `context/`" in content
        assert "vision and intent files" in content
        assert "safety and invariant files" in content
        assert "all other project context files" in content


def test_existing_scaffold_is_idempotent_and_preserves_bytes_and_inodes(tmp_path):
    from core.workspace_scaffolding import scaffold_project

    project = tmp_path / "project"
    (project / "context").mkdir(parents=True)
    index = project / "INDEX.md"
    context = project / "context" / "PROJECT.md"
    index.write_text("# Existing index\n", encoding="utf-8")
    context.write_text("# Existing project truth\n", encoding="utf-8")
    before = {
        path: (path.read_bytes(), path.stat().st_ino) for path in (index, context)
    }

    scaffold_project(project, name="Ignored", description="Ignored")
    scaffold_project(project, name="Ignored Again", description="Ignored Again")

    for path, (content, inode) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_ino == inode
    assert (project / "source").is_dir()
    assert (project / ".threads").is_dir()


@pytest.mark.parametrize(
    ("relative", "make_conflict"),
    [
        ("context", lambda path: path.write_text("not a directory", encoding="utf-8")),
        ("INDEX.md", lambda path: path.mkdir()),
    ],
)
def test_scaffold_rejects_type_conflicts_before_mutating(
    tmp_path,
    relative,
    make_conflict,
):
    from core.workspace_scaffolding import scaffold_workspace

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conflict = workspace / relative
    make_conflict(conflict)

    with pytest.raises(ValueError, match="Canonical scaffold"):
        scaffold_workspace(workspace)

    assert _names(workspace) == {relative}


def test_scaffold_rejects_nested_canonical_symlink_before_mutating(tmp_path):
    from core.workspace_scaffolding import scaffold_workspace

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "shared").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        scaffold_workspace(workspace)

    assert _names(workspace) == {"shared"}
    assert _names(outside) == set()


def test_new_scaffold_is_staged_and_removed_when_generation_fails(
    tmp_path,
    monkeypatch,
):
    from core import workspace_scaffolding

    original_write = Path.write_text

    def fail_on_claude(path: Path, *args, **kwargs):
        if path.name == "CLAUDE.md":
            raise OSError("simulated write failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_claude)
    workspace = tmp_path / "workspace"

    with pytest.raises(OSError, match="simulated write failure"):
        workspace_scaffolding.scaffold_workspace(workspace)

    assert not workspace.exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_scaffold_rolls_back_only_entries_published_by_failed_merge(
    tmp_path,
    monkeypatch,
):
    from core import workspace_scaffolding

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    keep = workspace / "keep.md"
    keep.write_text("keep", encoding="utf-8")
    real_rename = workspace_scaffolding.atomic_rename_noreplace
    publications = 0

    def fail_second_publish(source: Path, destination: Path):
        nonlocal publications
        if destination.parent == workspace:
            publications += 1
            if publications == 2:
                raise OSError("simulated publish failure")
        return real_rename(source, destination)

    monkeypatch.setattr(
        workspace_scaffolding,
        "atomic_rename_noreplace",
        fail_second_publish,
    )

    with pytest.raises(OSError, match="simulated publish failure"):
        workspace_scaffolding.scaffold_workspace(workspace)

    assert _names(workspace) == {"keep.md"}
    assert keep.read_text(encoding="utf-8") == "keep"
    assert not any("scaffold" in path.name for path in tmp_path.iterdir())


def test_scaffold_preserves_concurrent_target_and_never_replaces_its_file(
    tmp_path,
    monkeypatch,
):
    from core import workspace_scaffolding

    workspace = tmp_path / "workspace"
    real_rename = workspace_scaffolding.atomic_rename_noreplace
    injected = False

    def publish_after_concurrent_winner(source: Path, destination: Path):
        nonlocal injected
        if destination == workspace and not injected:
            injected = True
            workspace.mkdir()
            (workspace / "INDEX.md").write_text(
                "# Concurrent owner\n",
                encoding="utf-8",
            )
            (workspace / "keep.md").write_text("keep", encoding="utf-8")
        return real_rename(source, destination)

    monkeypatch.setattr(
        workspace_scaffolding,
        "atomic_rename_noreplace",
        publish_after_concurrent_winner,
    )

    assert workspace_scaffolding.scaffold_workspace(workspace) == workspace

    assert (workspace / "INDEX.md").read_text(encoding="utf-8") == (
        "# Concurrent owner\n"
    )
    assert (workspace / "keep.md").read_text(encoding="utf-8") == "keep"
    assert (workspace / "context").is_dir()
    assert (workspace / "shared" / "INDEX.md").is_file()


def test_failed_merge_does_not_delete_concurrent_content_inside_owned_directory(
    tmp_path,
    monkeypatch,
):
    from core import workspace_scaffolding

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_rename = workspace_scaffolding.atomic_rename_noreplace
    injected = False

    def inject_then_fail(source: Path, destination: Path):
        nonlocal injected
        result = real_rename(source, destination)
        if destination == workspace / ".threads" and not injected:
            injected = True
            (destination / "concurrent.txt").write_text(
                "preserve me",
                encoding="utf-8",
            )
            raise OSError("simulated late failure")
        return result

    monkeypatch.setattr(
        workspace_scaffolding,
        "atomic_rename_noreplace",
        inject_then_fail,
    )

    with pytest.raises(RuntimeError, match="compensation"):
        workspace_scaffolding.scaffold_workspace(workspace)

    assert (workspace / ".threads" / "concurrent.txt").read_text(
        encoding="utf-8"
    ) == "preserve me"
