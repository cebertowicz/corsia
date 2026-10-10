"""One-time startup conversion of the retired skill frontmatter key ``maestro:`` to ``corsia:``.

Readers read only ``corsia`` (``core.skill_validation.FRONTMATTER_KEY``) and
report a CORSIA block filed under any other key as a validation error, so a
skill this pass does not convert fails loudly in the skill diagnostics instead of
losing its metadata. The pass rewrites the key in every ``SKILL.md`` the skills
registry discovers on disk: the workspace and each project, their
``context/roles/<role>`` folders, and every ``.threads/<id>`` tray beneath them.
The generated System projection (``<workspace>/.corsia``) is never one of those
roots; it is regenerated from the packaged System content.

It runs at startup, before the guidance sweep commits outside edits, until one
pass completes on this install; the ``skill_frontmatter_key_conversion`` setting
records that pass and every later start skips it. A pass that raises or cannot
resolve a project or role root is not recorded, so it runs again on the next start. Nothing written afterwards is
converted: validation names the block to move. Only the key on its one top-level
frontmatter line changes; the body and every other byte stay as they were, and a
rewrite is kept only when the reader then sees the same block under the new key.
A file with both keys, a symlinked skill, or a key the line rewrite cannot place
is logged and left unchanged, and validation reports its skills. Remove this module in the release after the one that first
ships it (owner: Tech Debt); an install that skips that release still gets the
validation error, never a silent drop.
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, get_args

from loguru import logger

from core.role_context_sources import RoleName, role_directory
from core.skill_resolution import project_dir
from core.skill_scanning import skill_files
from core.skill_validation import FRONTMATTER_KEY, frontmatter_span, validate_skill_text
from core.workspace_layout import THREADS_DIR, resolve_workspace_root
from infra.database import get_db
from infra.settings import get_setting, set_setting

RETIRED_KEY = "maestro"
COMPLETED_SETTING = "skill_frontmatter_key_conversion"
_KEY_LINE = re.compile(rf"^([\"']?){RETIRED_KEY}\1:(?=\s|$)", re.MULTILINE)

Outcome = Literal["converted", "unchanged", "refused"]


@dataclass(frozen=True, slots=True)
class SkillKeyConversionResult:
    """Counts from the one skill frontmatter key conversion pass on this install."""

    converted: int
    refused: int
    unresolved_roots: int = 0


def rewrite_skill_key(text: str) -> tuple[str, str | None]:
    """Return ``text`` with its frontmatter key renamed, or unchanged and why it was refused."""
    before = validate_skill_text(text).metadata
    if RETIRED_KEY not in before:
        return text, None
    if FRONTMATTER_KEY in before:
        return text, f"frontmatter has both {RETIRED_KEY} and {FRONTMATTER_KEY} keys"
    span = frontmatter_span(text)
    matches = list(_KEY_LINE.finditer(text, *span)) if span else []
    if len(matches) != 1:
        return text, f"the {RETIRED_KEY} key is not on one plain top-level frontmatter line"
    key_start = matches[0].start() + len(matches[0].group(1))
    converted = text[:key_start] + FRONTMATTER_KEY + text[key_start + len(RETIRED_KEY):]
    expected = {FRONTMATTER_KEY if key == RETIRED_KEY else key: value for key, value in before.items()}
    if validate_skill_text(converted).metadata != expected:
        return text, "the rewritten frontmatter does not read back as the same block"
    return converted, None


def _scope_roots(workspace: Path, db, unresolved: list[str]) -> list[Path]:
    """The workspace root and every project root the registry resolves from the database."""
    roots = [resolve_workspace_root(workspace)]
    for row in db.execute("SELECT id FROM projects ORDER BY id").fetchall():
        try:
            root = project_dir(str(row[0]), workspace, db)
        except (OSError, ValueError) as exc:
            logger.warning("skill key conversion skipped project_id={}: {}", row[0], exc)
            unresolved.append(f"project {row[0]}")
            continue
        if root is not None:
            roots.append(root.resolve(strict=False))
    return list(dict.fromkeys(roots))


def _skill_roots(scope_root: Path, unresolved: list[str]) -> list[Path]:
    """The scope root, its role folders and its thread trays: each root holding ``skills/``."""
    roots = [scope_root]
    for role in get_args(RoleName):
        try:
            roots.append(role_directory(scope_root, role))
        except RuntimeError as exc:
            logger.warning("skill key conversion skipped role {} under {}: {}", role, scope_root, exc)
            unresolved.append(f"role {role} under {scope_root}")
    threads = scope_root / THREADS_DIR
    if threads.is_dir() and not threads.is_symlink():
        roots.extend(sorted(tray for tray in threads.iterdir() if tray.is_dir() and not tray.is_symlink()))
    return roots


def _replace(path: Path, expected: bytes, raw: bytes) -> bool:
    """Atomically swap in ``raw`` with the file's mode; False when it changed since it was read."""
    mode = stat.S_IMODE(path.stat().st_mode)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(raw)
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, mode)
        if path.read_bytes() != expected:
            return False
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _refuse(path: Path, reason: str) -> Outcome:
    logger.warning("skill frontmatter key conversion refused path={}: {}", path, reason)
    return "refused"


def _convert_file(path: Path) -> Outcome:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _refuse(path, f"unreadable: {exc}")
    converted, reason = rewrite_skill_key(text)
    if reason is not None:
        return _refuse(path, reason)
    if converted == text:
        return "unchanged"
    if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
        return _refuse(path, f"symlinked skill; convert {path.resolve()} where it lives")
    try:
        if not _replace(path, raw, converted.encode("utf-8")):
            return _refuse(path, "changed while converting")
    except OSError as exc:
        return _refuse(path, f"not written: {exc}")
    return "converted"


def convert_legacy_skill_keys(workspace: Path, *, db=None) -> SkillKeyConversionResult | None:
    """Rewrite the retired key in every discovered skill file once; None once it has run."""
    if db is None:
        with closing(get_db()) as connection:
            return convert_legacy_skill_keys(workspace, db=connection)
    if get_setting(COMPLETED_SETTING, db=db) is not None:
        return None
    counts: dict[Outcome, int] = {"converted": 0, "unchanged": 0, "refused": 0}
    unresolved: list[str] = []
    for scope_root in _scope_roots(workspace, db, unresolved):
        for root in _skill_roots(scope_root, unresolved):
            for path in skill_files(root):
                counts[_convert_file(path)] += 1
    result = SkillKeyConversionResult(
        converted=counts["converted"], refused=counts["refused"], unresolved_roots=len(unresolved)
    )
    if unresolved:
        logger.warning(
            "skill frontmatter key conversion incomplete, retrying next start: converted={} unresolved={}",
            result.converted,
            ", ".join(unresolved),
        )
        return result
    set_setting(
        COMPLETED_SETTING,
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "converted": result.converted,
            "refused": result.refused,
        },
        db=db,
    )
    logger.info(
        "skill frontmatter key conversion completed: converted={} refused={}",
        result.converted,
        result.refused,
    )
    return result
