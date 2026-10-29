"""The workflow shipped with the GitHub screening rule set."""

import hashlib
from pathlib import Path

from core.injection_rules import injection_rule_text
from core.skill_registry_types import SkillRow
from core.skill_validation import validate_skill_file

WORKFLOW_DIR = Path(__file__).parent / "github-pr-screen"
WORKFLOW_NAME = "github-pr-screen"


def version() -> str:
    """Bind the bundled workflow and shared instructions to exact content."""
    content = ((WORKFLOW_DIR / "SKILL.md").read_bytes() + b"\0"
               + (WORKFLOW_DIR / "corsia.yaml").read_bytes() + b"\0"
               + (WORKFLOW_DIR.parent / "stage.py").read_bytes() + b"\0"
               + (WORKFLOW_DIR.parent / "floor.py").read_bytes() + b"\0"
               + (WORKFLOW_DIR.parent / "result.py").read_bytes() + b"\0"
               + injection_rule_text().encode())
    return hashlib.sha256(content).hexdigest()


def workflow(project_id: str) -> SkillRow:
    body = WORKFLOW_DIR / "SKILL.md"
    checked = validate_skill_file(body)
    if not checked.ok:
        raise ValueError("the bundled GitHub screen workflow is invalid: " + "; ".join(checked.errors))
    return SkillRow(name=WORKFLOW_NAME, description=checked.description, scope="project", scope_id=project_id,
                    path=WORKFLOW_DIR, body_path=body, metadata=checked.metadata, status="included",
                    corsia=checked.corsia)
