"""Split rendered project context so its header is an accounted row (#1241)."""

PROJECT_FILES_MARKER = "\n\n## "


def split_project_header(rendered: str) -> tuple[str, str]:
    """Split into header and file sections; the halves rejoin byte for byte."""
    at = rendered.find(PROJECT_FILES_MARKER)
    return (rendered, "") if at < 0 else (rendered[:at], rendered[at:])


def project_parts(project_id: str, rendered: str) -> list[tuple[str, str, str, str, bool]]:
    """Accounted parts as (component id, label, reference, content, joined to the previous)."""
    header, files = split_project_header(rendered)
    ref = "project://" + project_id + "/"
    parts = [(f"project-header:{project_id}", "Project header", ref + "header", header, False)] if files else []
    parts.append((f"project:{project_id}", "Project context", ref + "rendered-context", files or header, bool(files)))
    return parts
