#!/usr/bin/env python3
"""Check that the packaged System tier is closed over its skill references.

The System tier (``backend/prompts/system/v1``) must ship every skill its own
text depends on. The check computes that set from the text and compares it
with the skills the manifest lists.

1. Declaration sources are every ``kind: "context"`` System manifest file and
   the conductor core and CORSIA agent prompts that consume System skills.
2. The skill pool is the System skills.
3. Seed: every declared ``skill `name` `` reference in those sources.
   Declarations are the closure contract: the checker validates them
   independently of the installed skill pool.
4. Close: repeat over every manifest-listed file belonging to each reached
   System skill.
5. Fail if a declaration is unavailable, the computed set differs from the
   manifest's skills, or a manifest-listed closure input is missing. Manifest
   freshness remains owned by ``generate_system_manifest.py``. This check
   proves declared dependencies, not arbitrary natural-language references.

Standard library only; needs no workspace. Exit 0 on pass, 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SYSTEM_REL = Path("backend/prompts/system/v1")
CONDUCTOR_CONTEXT_RELS = (
    Path("backend/prompts/conductor/core-v2.md"),
    Path("backend/prompts/conductor/corsia-agent-v2.md"),
)
_DECLARED_SKILL = re.compile(r"\bskill\s+`([a-z][a-z0-9-]*)`")


@dataclass
class ClosureResult:
    computed: set[str] = field(default_factory=set)
    manifest_skills: set[str] = field(default_factory=set)
    chains: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _skill_names(base: Path) -> dict[str, Path]:
    if not base.is_dir():
        return {}
    return {p.parent.name: p for p in sorted(base.glob("*/SKILL.md"))}


def _declared_skills(text: str) -> list[tuple[str, int]]:
    declarations: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _DECLARED_SKILL.finditer(line):
            declarations.append((match.group(1), lineno))
    return declarations


def _closure_sources(
    root: Path, context_files: list[Path], errors: list[str],
) -> list[Path]:
    """Add the conductor and CORSIA agent prompts that consume packaged System skills."""
    sources = list(context_files)
    for rel in CONDUCTOR_CONTEXT_RELS:
        context = root / rel
        if context.is_file():
            sources.append(context)
        else:
            errors.append(f"closure source missing on disk: {rel.as_posix()}")
    return sources


def compute(root: Path) -> ClosureResult:
    result = ClosureResult()
    system = root / SYSTEM_REL
    manifest_path = system / "MANIFEST.json"
    if not manifest_path.is_file():
        result.errors.append(f"missing manifest: {manifest_path.relative_to(root)}")
        return result
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    context_files: list[Path] = []
    skill_files: dict[str, list[Path]] = {}
    for entry in manifest.get("files", []):
        rel = entry["path"]
        if entry["kind"] not in {"context", "skill"}:
            continue  # coordination models are options, not closure inputs
        path = system / rel
        if not path.is_file():
            result.errors.append(f"manifest entry missing on disk: {rel}")
            continue
        if entry["kind"] == "context":
            context_files.append(path)
        elif entry["kind"] == "skill":
            skill = Path(rel).parts[1]
            if Path(rel).parts[2:] == ("SKILL.md",):
                result.manifest_skills.add(skill)  # an asset-only directory is not a skill
            skill_files.setdefault(skill, []).append(path)

    def label(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    seed_files = _closure_sources(root, context_files, result.errors)
    queue: list[str] = []

    def add(name: str, chain: str) -> None:
        if name in result.computed:
            return
        result.computed.add(name)
        result.chains[name] = chain
        queue.append(name)

    for path in seed_files:
        text = path.read_text(encoding="utf-8")
        for name, lineno in _declared_skills(text):
            add(name, f"{name} <- {label(path)}:{lineno}")

    while queue:
        current = queue.pop(0)
        paths = skill_files.get(current)
        if paths is None:
            result.errors.append(f"skill not found in pool: {current} ({result.chains[current]})")
            continue
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for name, lineno in _declared_skills(text):
                if name != current:
                    add(name, f"{name} <- {label(path)}:{lineno} <- {result.chains[current]}")

    unexpected = sorted(result.computed - result.manifest_skills)
    missing = sorted(result.manifest_skills - result.computed)
    for name in unexpected:
        result.errors.append(f"computed but not in manifest: {result.chains[name]}")
    for name in missing:
        result.errors.append(f"in manifest but not reachable: {name}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    result = compute(args.root)
    total = len(_skill_names(args.root / SYSTEM_REL / "skills"))
    print(f"System closure: {len(result.computed)} of {total} skills reachable; "
          f"manifest lists {len(result.manifest_skills)}")
    for name in sorted(result.computed):
        print(f"  {result.chains[name]}")
    for error in result.errors:
        print(f"FAIL: {error}", file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
