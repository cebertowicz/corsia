"""Path-mapping rules from the starter kit back to its workspace sources.

Verified empirically against the live trees (2026-09-17) rather than taken
on faith from the kit's own README table:

- ``context/**`` sources from ``<workspace>/context/**`` (the workspace-root
  context), NOT ``<workspace>/projects/corsia/context/**`` -- the project
  context tree holds unrelated conductor files (``PROJECT.md``,
  ``VISION-CORE.md``, ...). The README table's own "install to
  ``<workspace>/context/``" line agrees; the source is the same root.
- ``shared/standards/**`` sources from
  ``<workspace>/projects/corsia/shared/standards/**`` -- there is no
  workspace-root ``shared/standards/`` at all.
- ``skills/**`` holds the kit's non-System skills. Most source from the global
  ``<workspace>/skills/<name>/**`` tree; ``design-interrogation`` sources from
  ``<workspace>/projects/corsia/skills/<name>/**``. Resolution remains
  per-name so the mapping follows the file's actual canonical location.
- ``project-skills/**`` sources from
  ``<workspace>/projects/corsia/skills/<name>/**``, but all three files
  (``sdlc-process``, ``ci-gates-locally``, ``corsia-test-environment``) are
  deliberately rewritten contributor editions -- a two-runtime operating
  model instead of the maintainers' three, no internal-only concepts --
  rather than scrubbed copies. A mechanical diff-after-scrub verdict does
  not fit them; only source existence is checked, tracked as "adapted".
- Three kit files have no workspace original at all and are kit-native:
  the kit's own ``README.md`` and the two ``templates/*.md`` files (generic
  templates filled in per new contributor project, not copied from one
  canonical source).
- The System tier (``context/{10,20,30,40}-*.md``, ``HOST-ACCESS-BOUNDARY.md``,
  ``CORSIA-API-ACCESS.md``, the role contracts and the core skills) ships in
  ``backend/prompts/system/v1/`` and has no kit copy. The workspace copies of
  those files are duplicates that slice 3 of #939 reports; this check does not
  compare them, and ``scripts/check_system_closure.py`` owns the System tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

KIT_ROOT_REL = Path("docs/contributing/bootstrap/starter-kit")

KIT_NATIVE_FILES = frozenset(
    {
        "README.md",
        "templates/PROJECT.md",
        "templates/RESPONSIBILITY.md",
    }
)

_ADAPTED_PROJECT_SKILLS = frozenset(
    {"sdlc-process", "ci-gates-locally", "corsia-test-environment"}
)


@dataclass(frozen=True)
class Resolution:
    """Where one starter-kit file's content is supposed to come from."""

    kind: str  # "kit_native" | "copy" | "adapted" | "unmapped"
    source: Path | None


def resolve_source(kit_rel: PurePosixPath, workspace: Path) -> Resolution:
    """Map one starter-kit relative path to its workspace source, if any."""
    rel = kit_rel.as_posix()
    if rel in KIT_NATIVE_FILES:
        return Resolution("kit_native", None)
    if rel.startswith("context/"):
        return Resolution("copy", workspace / rel)
    if rel.startswith("shared/standards/"):
        return Resolution("copy", workspace / "projects" / "corsia" / rel)
    if rel.startswith("skills/"):
        return _resolve_skill(kit_rel, workspace)
    if rel.startswith("project-skills/"):
        return _resolve_project_skill(kit_rel, workspace)
    return Resolution("unmapped", None)


def _resolve_skill(kit_rel: PurePosixPath, workspace: Path) -> Resolution:
    """``skills/<name>/rest`` -> global skills, else project-scoped skills."""
    parts = kit_rel.parts  # ("skills", name, ...rest)
    name, rest = parts[1], Path(*parts[2:])
    for base in (workspace / "skills", workspace / "projects" / "corsia" / "skills"):
        candidate = base / name / rest
        if candidate.exists():
            return Resolution("copy", candidate)
    return Resolution("copy", workspace / "skills" / name / rest)


def _resolve_project_skill(kit_rel: PurePosixPath, workspace: Path) -> Resolution:
    """``project-skills/<name>/rest`` -> project skills; some are adapted."""
    parts = kit_rel.parts  # ("project-skills", name, ...rest)
    name, rest = parts[1], Path(*parts[2:])
    source = workspace / "projects" / "corsia" / "skills" / name / rest
    kind = "adapted" if name in _ADAPTED_PROJECT_SKILLS else "copy"
    return Resolution(kind, source)
