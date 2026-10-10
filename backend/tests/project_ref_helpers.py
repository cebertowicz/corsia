"""Helpers for tests that need a project row with a logical workspace path."""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ID = "workspace-123"


def select_workspace(monkeypatch, root: Path) -> None:
    """Make ``root`` the selected workspace so logical project refs resolve."""
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(
        "infra.config.WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id=WORKSPACE_ID, root=Path(root).resolve(), name="Primary"
        ),
        raising=False,
    )
    monkeypatch.setattr("infra.config.WORKSPACE", Path(root).resolve(), raising=False)


def project_ref(slug: str) -> str:
    """Return the logical reference stored for ``projects/<slug>``."""
    return "workspace:/" f"/{WORKSPACE_ID}/projects/{slug}"


def write_project_context(root: Path, slug: str) -> Path:
    """Create the required project context files for ``projects/<slug>``."""
    project = Path(root) / "projects" / slug
    (project / "context").mkdir(parents=True, exist_ok=True)
    for name in ("PROJECT.md", "COORDINATION-MODEL.md"):
        (project / "context" / name).write_text(f"# {name}\n", encoding="utf-8")
    return project
