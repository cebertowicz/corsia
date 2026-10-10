"""Tests for scripts/check_system_closure.py (issue #939, System tier PR 4)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_system_closure import compute, main  # noqa: E402

SYSTEM = "backend/prompts/system/v1"
KIT = "docs/contributing/bootstrap/starter-kit"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(root: Path, context: str, system_skills: dict[str, str],
           kit_skills: dict[str, str] | None = None,
           project_skills: dict[str, str] | None = None,
           system_references: dict[str, dict[str, str]] | None = None,
           conductor: str = "Use skill `conductor-bootstrap`.\n") -> None:
    files = []

    def add(rel: str, kind: str, text: str) -> None:
        _write(root, f"{SYSTEM}/{rel}", text)
        data = text.encode("utf-8")
        files.append({"bytes": len(data), "kind": kind, "path": rel,
                      "sha256": hashlib.sha256(data).hexdigest()})

    add("context/10-INTENT-AUTHORITY.md", "context", context)
    for name, text in system_skills.items():
        add(f"skills/{name}/SKILL.md", "skill", text)
    for skill, references in (system_references or {}).items():
        for name, text in references.items():
            add(f"skills/{skill}/references/{name}", "skill", text)
    for name, text in (kit_skills or {}).items():
        _write(root, f"{KIT}/skills/{name}/SKILL.md", text)
    for name, text in (project_skills or {}).items():
        _write(root, f"{KIT}/project-skills/{name}/SKILL.md", text)
    _write(root, "backend/prompts/conductor/core-v2.md", conductor)
    _write(root, f"{SYSTEM}/MANIFEST.json", json.dumps({"files": files}))


def _baseline(extra: dict[str, str] | None = None) -> dict[str, str]:
    skills = {"conductor-bootstrap": "# conductor-bootstrap\n"}
    skills.update(extra or {})
    return skills


def test_closed_tier_passes(tmp_path: Path):
    _build(
        tmp_path,
        "Use skill `decision-brief` before messaging.\n",
        _baseline({"decision-brief": "See skill `decide-or-raise`.\n", "decide-or-raise": "x\n"}),
        kit_skills={"security-review": "unrelated\n"},
    )
    result = compute(tmp_path)
    assert result.errors == []
    assert result.computed == {
        "conductor-bootstrap", "decision-brief", "decide-or-raise",
    }
    assert main(["--root", str(tmp_path)]) == 0


def test_conductor_prompt_declarations_seed_closure(tmp_path: Path):
    _build(
        tmp_path, "x\n", _baseline({"conductor-only": "x\n"}),
        conductor="Use skill `conductor-only`.\n",
    )

    result = compute(tmp_path)

    assert "conductor-only" in result.computed
    assert "backend/prompts/conductor/core-v2.md:1" in result.chains["conductor-only"]


def test_transitive_chain_is_reported(tmp_path: Path):
    _build(tmp_path, "Use skill `decision-brief`.\n",
           _baseline({"decision-brief": "line1\nsee skill `decide-or-raise`\n", "decide-or-raise": "x\n"}))
    chain = compute(tmp_path).chains["decide-or-raise"]
    assert "decision-brief/SKILL.md:2" in chain
    assert "10-INTENT-AUTHORITY.md:1" in chain


def test_skill_missing_from_manifest_fails(tmp_path: Path):
    _build(tmp_path, "Use skill `decision-brief`.\n", _baseline(),
           kit_skills={"decision-brief": "kit copy\n"})
    result = compute(tmp_path)
    assert any("computed but not in manifest" in e for e in result.errors)
    assert main(["--root", str(tmp_path)]) == 1


def test_unreachable_manifest_skill_fails(tmp_path: Path):
    _build(tmp_path, "nothing named\n", _baseline({"orphan-skill": "x\n"}))
    assert any("not reachable: orphan-skill" in e for e in compute(tmp_path).errors)


def test_declared_project_skill_fails(tmp_path: Path):
    _build(tmp_path, "Follow skill `sdlc-process`.\n", _baseline(),
           project_skills={"sdlc-process": "contributor only\n"})
    assert any("project-skills" in e for e in compute(tmp_path).errors)


def test_unshipped_declared_context_skill_fails(tmp_path: Path):
    _build(tmp_path, "Use skill `misspelled-skill` before messaging.\n", _baseline())
    assert any("skill not found in pool: misspelled-skill" in error
               for error in compute(tmp_path).errors)


def test_unshipped_declared_transitive_skill_fails(tmp_path: Path):
    _build(
        tmp_path, "Use skill `task-discipline`.\n",
        _baseline({"task-discipline": "Use skill `missing-skill`.\n"}),
    )
    assert any("skill not found in pool: missing-skill" in error
               for error in compute(tmp_path).errors)


def test_unshipped_declared_reference_skill_fails(tmp_path: Path):
    _build(
        tmp_path,
        "x\n",
        _baseline(),
        system_references={
            "conductor-bootstrap": {"overview.md": "Use skill `absent-skill`.\n"},
        },
    )
    assert any("skill not found in pool: absent-skill" in error
               for error in compute(tmp_path).errors)


def test_hyphenated_non_skill_prose_is_ignored(tmp_path: Path):
    _build(tmp_path, "The `system-tier` design is complete.\n", _baseline())
    assert compute(tmp_path).errors == []


def test_undeclared_installed_skill_name_is_ignored(tmp_path: Path):
    _build(
        tmp_path,
        "The `decision-brief` document is ready.\n",
        _baseline(),
        kit_skills={"decision-brief": "unrelated\n"},
    )
    assert compute(tmp_path).errors == []


def test_missing_closure_input_fails(tmp_path: Path):
    _build(tmp_path, "x\n", _baseline())
    (tmp_path / SYSTEM / "skills/conductor-bootstrap/SKILL.md").unlink()
    errors = compute(tmp_path).errors
    assert any("missing on disk" in e for e in errors)


def test_missing_conductor_prompt_fails(tmp_path: Path):
    _build(tmp_path, "x\n", _baseline())
    (tmp_path / "backend/prompts/conductor/core-v2.md").unlink()

    errors = compute(tmp_path).errors

    assert any("closure source missing on disk" in error for error in errors)


def test_missing_manifest_fails(tmp_path: Path):
    assert main(["--root", str(tmp_path)]) == 1
