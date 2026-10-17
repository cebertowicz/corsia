"""A skill that still carries the legacy top-level ``corsia:`` block loads unchanged.

Pins the pre-sidecar behavior for the real packaged decision-brief data folded back
into the legacy block, and a user thread skill with an inline block: validation
fields, effective workflows and workflow versions. Sidecar versions are pinned beside
them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.skill_scanning import scan_scope
from core.skill_validation import (
    parse_frontmatter,
    validate_skill_bundle,
    validate_skill_file,
    validate_skill_text,
)
from core.skills_registry import SkillResolution
from infra.system_package import system_package_dir
from tools import workflow_task_fields
from tools.workflow_task_fields import _workflow_version, effective_workflows

THREAD_SKILL = """---
name: thread-flow
description: A user thread skill with the inline block.
corsia:
  handler:
    kind: instruction
  workflow:
    steps:
      - id: plan
        title: Plan it
        required: false
      - id: ship
        title: Ship it
        required: true
        check: shipped
        after: [plan]
---
# Thread flow
"""


def _fields(result):
    return (result.ok, result.name, result.description, result.metadata, result.body, result.errors)


def _skills_root(tmp_path: Path, name: str, text: str) -> Path:
    folder = tmp_path / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def decision_brief_text() -> str:
    """The packaged decision-brief in its legacy form: sidecar data inline, no pointer."""
    folder = system_package_dir() / "skills/decision-brief"
    meta, body = parse_frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
    meta.pop("metadata")
    meta["corsia"] = yaml.safe_load((folder / "corsia.yaml").read_text(encoding="utf-8"))
    return f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n{body}"


@pytest.mark.parametrize("name", ["decision-brief", "thread-flow"])
def test_legacy_block_validates_identically_through_every_entry(tmp_path, decision_brief_text, name):
    text = decision_brief_text if name == "decision-brief" else THREAD_SKILL
    path = _skills_root(tmp_path, name, text) / "skills" / name / "SKILL.md"

    legacy = validate_skill_text(text)

    assert legacy.ok, legacy.errors
    assert _fields(validate_skill_file(path)) == _fields(legacy)
    assert _fields(validate_skill_bundle(text, None)) == _fields(legacy)
    assert legacy.corsia == legacy.metadata["corsia"]
    assert validate_skill_file(path).corsia == legacy.metadata["corsia"]


def _effective(monkeypatch, rows):
    monkeypatch.setattr(
        workflow_task_fields, "resolve_effective_skills",
        lambda **_kwargs: SkillResolution(rows, [], []),
    )
    return effective_workflows(SimpleNamespace(thread_id="owner", project_id=None), None)


def test_effective_workflows_from_scanned_legacy_skills(tmp_path, monkeypatch, decision_brief_text):
    system = scan_scope(
        _skills_root(tmp_path / "system", "decision-brief", decision_brief_text), "system", "v1",
    )
    thread = scan_scope(_skills_root(tmp_path / "thread", "thread-flow", THREAD_SKILL), "thread", "t")

    workflows = _effective(monkeypatch, [*system, *thread])

    assert sorted(workflows) == ["decision-brief", "thread-flow"]
    assert workflows["decision-brief"].required_steps == ("long-form", "brief", "decision")
    assert workflows["thread-flow"].required_steps == ("ship",)
    assert workflows["decision-brief"].skill is system[0]
    assert workflows["decision-brief"].version is None


def test_effective_workflows_follow_a_sidecar_skill(tmp_path, monkeypatch):
    folder = tmp_path / "skills" / "sidecar-flow"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: sidecar-flow\ndescription: Sidecar skill.\nmetadata:\n"
        "  corsia-data: corsia.yaml\n  corsia-format: '1'\n---\n# Sidecar\n",
        encoding="utf-8",
    )
    (folder / "corsia.yaml").write_text(
        "workflow:\n  steps:\n    - {id: only, required: true, check: done}\n", encoding="utf-8",
    )

    workflows = _effective(monkeypatch, scan_scope(tmp_path, "thread", "t"))

    assert workflows["sidecar-flow"].required_steps == ("only",)


def test_system_version_of_a_skill_without_a_sidecar_keeps_its_old_form(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=False)

    assert _workflow_version(SimpleNamespace(name="flow", scope="system")) == "system:skills/flow/SKILL.md:v9:aaa"


def test_packaged_decision_brief_version_covers_its_recorded_sidecar():
    from core.system_tier import system_tier_manifest

    manifest = system_tier_manifest()
    digests = {i.path: i.sha256 for i in manifest.files}

    assert _workflow_version(SimpleNamespace(name="decision-brief", scope="system")) == (
        f"system:skills/decision-brief/SKILL.md:{manifest.version}:"
        f"{digests['skills/decision-brief/SKILL.md']}+{digests['skills/decision-brief/corsia.yaml']}"
    )


def _fake_system(monkeypatch, tmp_path, *, sidecar: bool, mismatched: bool = False):
    package = tmp_path / "package"
    (package / "skills" / "flow").mkdir(parents=True)
    (package / "skills/flow/SKILL.md").write_text("skill", encoding="utf-8")
    files = [SimpleNamespace(path="skills/flow/SKILL.md", sha256="aaa")]
    if sidecar:
        (package / "skills/flow/corsia.yaml").write_text("workflow: {}", encoding="utf-8")
        files.append(SimpleNamespace(path="skills/flow/corsia.yaml", sha256="bbb"))
    manifest = SimpleNamespace(
        files=files, version="v9", mismatches=files[1:] if mismatched else [],
    )
    monkeypatch.setattr(workflow_task_fields, "system_package_dir", lambda: package)
    monkeypatch.setattr("core.system_tier.system_tier_manifest", lambda: manifest)


def test_system_version_covers_the_sidecar_digest(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=True)

    version = _workflow_version(SimpleNamespace(name="flow", scope="system"))

    assert version == "system:skills/flow/SKILL.md:v9:aaa+bbb"


def test_system_version_is_unavailable_when_the_sidecar_drifts_from_the_manifest(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=True, mismatched=True)

    assert _workflow_version(SimpleNamespace(name="flow", scope="system")) is None


def _fake_git(monkeypatch, versions):
    monkeypatch.setattr(workflow_task_fields, "workspace_repository", lambda _root: ("repo", None))
    monkeypatch.setattr(workflow_task_fields, "file_version", lambda _repo, path: versions.get(Path(path).name))


def test_editable_version_of_a_legacy_skill_is_its_single_commit(monkeypatch, tmp_path):
    path = _skills_root(tmp_path, "thread-flow", THREAD_SKILL) / "skills/thread-flow/SKILL.md"
    _fake_git(monkeypatch, {"SKILL.md": "c1"})

    assert _workflow_version(SimpleNamespace(name="thread-flow", scope="thread", path=path)) == "c1"


def test_editable_version_covers_the_sidecar_commit(monkeypatch, tmp_path):
    path = _skills_root(tmp_path, "flow", "x") / "skills/flow/SKILL.md"
    (path.parent / "corsia.yaml").write_text("workflow: {}", encoding="utf-8")
    skill = SimpleNamespace(name="flow", scope="thread", path=path)

    _fake_git(monkeypatch, {"SKILL.md": "c1", "corsia.yaml": "c2·dirty"})
    assert _workflow_version(skill) == "c1+c2·dirty"
    _fake_git(monkeypatch, {"SKILL.md": "c1"})
    assert _workflow_version(skill) is None
