"""A sidecar skill loads the same through every entry; its workflow version covers the sidecar.

Pins validation fields, effective workflows and workflow versions for a skill whose CORSIA
data lives in ``corsia.yaml``, and for one without any, plus the packaged decision-brief's
recorded sidecar digest.
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
from tools import workflow_task_fields
from tools.workflow_task_fields import _workflow_version, effective_workflows
from tests.skill_sidecar_helpers import POINTER

THREAD_SKILL = f"---\nname: thread-flow\ndescription: A user thread skill.\n{POINTER}---\n# Thread flow\n"
THREAD_SIDECAR = """handler:
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
"""


def _fields(result):
    return (result.ok, result.name, result.description, result.metadata, result.body, result.errors)


def _skills_root(tmp_path: Path, name: str, text: str, sidecar: str | None = None) -> Path:
    folder = tmp_path / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    if sidecar is not None:
        (folder / "corsia.yaml").write_text(sidecar, encoding="utf-8")
    return tmp_path


def test_sidecar_skill_validates_identically_through_every_entry(tmp_path):
    path = _skills_root(tmp_path, "thread-flow", THREAD_SKILL, THREAD_SIDECAR) / "skills/thread-flow/SKILL.md"

    from_file = validate_skill_file(path)
    from_bundle = validate_skill_bundle(THREAD_SKILL, {"corsia.yaml": THREAD_SIDECAR})

    assert from_file.ok, from_file.errors
    assert _fields(from_file) == _fields(from_bundle)
    assert from_file.corsia == from_bundle.corsia
    assert from_file.corsia["handler"] == {"kind": "instruction"}
    assert validate_skill_text(THREAD_SKILL).corsia is None


def _effective(monkeypatch, rows):
    monkeypatch.setattr(
        workflow_task_fields, "resolve_effective_skills",
        lambda **_kwargs: SkillResolution(rows, [], []),
    )
    return effective_workflows(SimpleNamespace(thread_id="owner", project_id=None), None)


def test_effective_workflows_from_scanned_sidecar_skills(tmp_path, monkeypatch):
    thread = scan_scope(
        _skills_root(tmp_path / "thread", "thread-flow", THREAD_SKILL, THREAD_SIDECAR), "thread", "t",
    )

    workflows = _effective(monkeypatch, thread)

    assert sorted(workflows) == ["thread-flow"]
    assert [step["id"] for step in workflows["thread-flow"].steps if step.get("required")] == ["ship"]
    assert workflows["thread-flow"].skill is thread[0]
    assert workflows["thread-flow"].version is None


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

    assert [step["id"] for step in workflows["sidecar-flow"].steps] == ["only"]


def test_a_library_origin_key_beside_the_workflow_still_resolves(tmp_path, monkeypatch):
    """A skill installed from the library records its ``origin:`` beside ``workflow:``."""
    folder = tmp_path / "skills" / "library-flow"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: library-flow\ndescription: Library skill.\nmetadata:\n"
        "  corsia-data: corsia.yaml\n  corsia-format: '1'\n---\n# Library\n",
        encoding="utf-8",
    )
    (folder / "corsia.yaml").write_text(
        "origin:\n  library: corsia-library\n  name: library-flow\n  version: 1.2.0\n"
        "workflow:\n  steps:\n    - {id: only, required: true, check: done}\n",
        encoding="utf-8",
    )

    workflows = _effective(monkeypatch, scan_scope(tmp_path, "thread", "t"))

    assert [step["id"] for step in workflows["library-flow"].steps] == ["only"]


def test_packaged_decision_brief_version_covers_its_recorded_sidecar():
    from core.system_tier import system_tier_manifest

    manifest = system_tier_manifest()
    digests = {i.path: i.sha256 for i in manifest.files}

    assert _workflow_version(SimpleNamespace(name="decision-brief", scope="system")) == (
        f"system:skills/decision-brief/SKILL.md:{manifest.version}:"
        f"{digests['skills/decision-brief/SKILL.md']}+{digests['skills/decision-brief/corsia.yaml']}"
    )


def _fake_system(
    monkeypatch, tmp_path, *, sidecar: bool, mismatched: bool = False, pointer: bool | None = None,
):
    package = tmp_path / "package"
    (package / "skills" / "flow").mkdir(parents=True)
    skill_md = THREAD_SKILL if (sidecar if pointer is None else pointer) else "skill"
    (package / "skills/flow/SKILL.md").write_text(skill_md, encoding="utf-8")
    files = [SimpleNamespace(path="skills/flow/SKILL.md", sha256="aaa")]
    if sidecar:
        (package / "skills/flow/corsia.yaml").write_text("workflow: {}", encoding="utf-8")
        files.append(SimpleNamespace(path="skills/flow/corsia.yaml", sha256="bbb"))
    manifest = SimpleNamespace(
        files=files, version="v9", mismatches=files[1:] if mismatched else [],
    )
    monkeypatch.setattr(workflow_task_fields, "system_package_dir", lambda: package)
    monkeypatch.setattr("core.system_tier.system_tier_manifest", lambda: manifest)


def test_system_version_of_a_skill_without_a_sidecar_is_its_one_digest(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=False)

    version = _workflow_version(SimpleNamespace(name="flow", scope="system"))

    assert version == "system:skills/flow/SKILL.md:v9:aaa"


def test_system_version_covers_the_sidecar_digest(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=True)

    version = _workflow_version(SimpleNamespace(name="flow", scope="system"))

    assert version == "system:skills/flow/SKILL.md:v9:aaa+bbb"


def test_system_version_ignores_a_listed_sidecar_the_skill_does_not_point_at(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=True, pointer=False)

    assert _workflow_version(SimpleNamespace(name="flow", scope="system")) == "system:skills/flow/SKILL.md:v9:aaa"


def test_system_version_is_unavailable_when_the_sidecar_drifts_from_the_manifest(monkeypatch, tmp_path):
    _fake_system(monkeypatch, tmp_path, sidecar=True, mismatched=True)

    assert _workflow_version(SimpleNamespace(name="flow", scope="system")) is None


def _fake_git(monkeypatch, versions):
    monkeypatch.setattr(workflow_task_fields, "workspace_repository", lambda _root: ("repo", None))
    monkeypatch.setattr(workflow_task_fields, "file_version", lambda _repo, path: versions.get(Path(path).name))


def test_editable_version_of_a_skill_without_a_sidecar_is_its_single_commit(monkeypatch, tmp_path):
    path = _skills_root(tmp_path, "plain", "---\nname: plain\ndescription: Plain.\n---\n") / "skills/plain/SKILL.md"
    _fake_git(monkeypatch, {"SKILL.md": "c1"})

    assert _workflow_version(SimpleNamespace(name="plain", scope="thread", path=path)) == "c1"


def test_editable_version_of_an_unborn_repository_is_unavailable(monkeypatch, tmp_path):
    """A Linux shell launch leaves a repository with no commit; that must read as no version, not a SHA."""
    import subprocess

    from infra import config

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=str(tmp_path), check=True, timeout=30)
    path = _skills_root(tmp_path, "plain", "---\nname: plain\ndescription: Plain.\n---\n") / "skills/plain/SKILL.md"

    assert workflow_task_fields._editable_workflow_version(path) is None


def test_editable_version_covers_the_sidecar_commit(monkeypatch, tmp_path):
    path = _skills_root(tmp_path, "flow", THREAD_SKILL, "workflow: {}") / "skills/flow/SKILL.md"
    skill = SimpleNamespace(name="flow", scope="thread", path=path)

    _fake_git(monkeypatch, {"SKILL.md": "c1", "corsia.yaml": "c2·dirty"})
    assert _workflow_version(skill) == "c1+c2·dirty"
    _fake_git(monkeypatch, {"SKILL.md": "c1"})
    assert _workflow_version(skill) is None


def test_editable_version_ignores_a_stray_sidecar_nothing_points_at(monkeypatch, tmp_path):
    path = _skills_root(tmp_path, "plain", "---\nname: plain\ndescription: Plain.\n---\n", "workflow: {}") / "skills/plain/SKILL.md"
    skill = SimpleNamespace(name="plain", scope="thread", path=path)

    _fake_git(monkeypatch, {"SKILL.md": "c1", "corsia.yaml": "c2"})
    assert _workflow_version(skill) == "c1"
    _fake_git(monkeypatch, {"SKILL.md": "c1"})
    assert _workflow_version(skill) == "c1"
