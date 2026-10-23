"""One-time startup conversion of a skill's top-level ``corsia:`` block into a ``corsia.yaml`` sidecar.

Readers take a skill's CORSIA data (handler, workflow, origin) from the ``corsia.yaml``
beside ``SKILL.md``, which the frontmatter points at with
``metadata: {corsia-data: corsia.yaml, corsia-format: '1'}``. A block holding ``lesson`` or
``capture`` is lesson data: it stays in ``SKILL.md`` untouched and never gets a sidecar.
Any other block is retired, and the skill diagnostics report it. This pass moves a retired
block's data into the sidecar for every ``SKILL.md`` the skills registry discovers on disk:
the workspace and each project, their ``context/roles/<role>`` folders, and every
``.threads/<id>`` tray beneath them. The generated System projection (``<workspace>/.corsia``)
is never one of those roots; it is regenerated from the packaged System content.

It runs at startup, right after the skill key conversion, until one pass completes on this
install; the ``skill_corsia_data_conversion`` setting records that pass and every later start
skips it. A pass that raises or cannot resolve a project or role root is not recorded, so it
runs again on the next start. Nothing written afterwards is converted: validation names the
block to move. ``corsia.yaml`` is written first, then ``SKILL.md`` has the block's lines
removed and the two pointer keys added under its block-style ``metadata`` mapping (or a new
one); every other byte stays as it was, and a rewrite is kept only when the reader then
resolves the same data, name, description and other metadata. A block that holds only the
implied default handler (``handler: {kind: instruction}``) is dropped with no sidecar and no
pointer. A block beside a pointer at a sidecar with identical data (an interrupted
conversion) is finished by removing the block. A flow-style ``metadata``, a ``corsia.yaml``
holding different data, a symlink, or frontmatter the line rewrite cannot place is logged
and left unchanged, and validation reports the skill. Remove this module in the release
after the one that first ships it (owner: Context & Memory); an install that skips that
release still gets the validation error, never a silent drop.
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
from typing import Any, Literal, get_args

import yaml
from loguru import logger

from core.role_context_sources import RoleName, role_directory
from core.skill_resolution import project_dir
from core.skill_scanning import skill_files
from core.skill_sidecar import (
    BLOCK_ONLY_KEYS,
    FORMAT_KEY,
    POINTER_KEY,
    SIDECAR_FILE,
    SIDECAR_FORMAT,
    parse_sidecar,
    pointer_errors,
    read_sidecar,
)
from core.skill_validation import (
    FRONTMATTER_KEY,
    frontmatter_span,
    parse_frontmatter,
    validate_skill_file,
)
from core.workspace_layout import THREADS_DIR, resolve_workspace_root
from infra.database import get_db
from infra.settings import get_setting, set_setting

COMPLETED_SETTING = "skill_corsia_data_conversion"
_BLOCK_LINE = re.compile(rf"^([\"']?){FRONTMATTER_KEY}\1:(?=\s|$)")
_BARE_BLOCK_LINE = re.compile(rf"^{FRONTMATTER_KEY}:[ \t]*(?:#.*)?$")
_METADATA_LINE = re.compile(r"^([\"']?)metadata\1:[ \t]*(?:#.*)?$")
_DEFAULT_ONLY = {"handler": {"kind": "instruction"}}
_POINTER_KEYS = (POINTER_KEY, FORMAT_KEY)

Outcome = Literal["converted", "unchanged", "refused"]


@dataclass(frozen=True, slots=True)
class SkillDataConversionResult:
    """Counts from the one skill CORSIA data conversion pass on this install."""

    converted: int
    refused: int
    unresolved_roots: int = 0


@dataclass(frozen=True, slots=True)
class Conversion:
    """The rewrite of one ``SKILL.md``: unchanged, or refused with a reason."""

    text: str
    sidecar: str | None = None  # the corsia.yaml text to write first, when one is needed
    data: dict[str, Any] | None = None  # the CORSIA data the reader must resolve afterwards
    refused: str | None = None


def _refusal(text: str, reason: str) -> Conversion:
    return Conversion(text, refused=reason)


def _block_end(lines: list[str], start: int) -> int:
    """The index after the last non-blank line of the block opened at ``lines[start]``."""
    end = start + 1
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and lines[index][0] not in " \t":
            break
        if lines[index].strip():
            end = index + 1
    return end


def _sidecar_text(block_lines: list[str], block: dict[str, Any]) -> str:
    """The block unwrapped: its lines dedented (keeping comments) when they read back the same."""
    rest = block_lines[1:]
    if _BARE_BLOCK_LINE.match(block_lines[0].rstrip("\n")) and rest:
        indent = min(len(line) - len(line.lstrip(" ")) for line in rest if line.strip())
        candidate = "".join(line[indent:] if line.strip() else "\n" for line in rest)
        try:
            if yaml.safe_load(candidate) == block:
                return candidate
        except yaml.YAMLError:
            pass
    return yaml.safe_dump(block, sort_keys=False, allow_unicode=True)


def _pointer_lines(lines: list[str], meta: dict[str, Any]) -> tuple[int, list[str]] | str:
    """Where the pointer keys go in a block-style ``metadata`` mapping, or why they cannot."""
    metadata = meta["metadata"]
    found = [index for index, line in enumerate(lines) if _METADATA_LINE.match(line)]
    if not isinstance(metadata, dict) or len(found) != 1:
        return "metadata is not one plain block-style mapping"
    end = _block_end(lines, found[0])
    children = [line for line in lines[found[0] + 1:end] if not line.lstrip().startswith("#")]
    indent = children[0][: len(children[0]) - len(children[0].lstrip(" \t"))] if children else ""
    if not indent or "\t" in indent:
        return "metadata is not one plain block-style mapping"
    return end, [f"{indent}{POINTER_KEY}: {SIDECAR_FILE}\n", f"{indent}{FORMAT_KEY}: '{SIDECAR_FORMAT}'\n"]


def rewrite_skill_data(text: str, sidecar: str | None = None) -> Conversion:
    """Return ``text`` with its retired block moved to the sidecar, or why it was refused.

    ``sidecar`` is the ``corsia.yaml`` text already beside the skill, if any.
    """
    span = frontmatter_span(text) if FRONTMATTER_KEY in text else None
    if span is None:
        return Conversion(text)
    try:
        meta = parse_frontmatter(text)[0]
    except Exception:  # noqa: BLE001 - validation reports an unparseable frontmatter.
        return Conversion(text)
    block = meta.get(FRONTMATTER_KEY)
    if block is None or (isinstance(block, dict) and set(BLOCK_ONLY_KEYS) & set(block)):
        return Conversion(text)
    if not isinstance(block, dict):
        return _refusal(text, "the corsia block is not a mapping")
    start, end = span[0], span[1] + 1
    lines = text[start:end].splitlines(keepends=True)
    opened = [index for index, line in enumerate(lines) if _BLOCK_LINE.match(line)]
    if len(opened) != 1:
        return _refusal(text, "the corsia block is not on one plain top-level frontmatter line")
    first, last = opened[0], _block_end(lines, opened[0])
    declared, errors = pointer_errors(meta.get("metadata"))
    if errors:
        return _refusal(text, "; ".join(errors))
    existing = parse_sidecar(sidecar)[0] if sidecar is not None else None
    dropped = block == _DEFAULT_ONLY and not declared
    if (declared or sidecar is not None and not dropped) and existing != block:
        return _refusal(text, f"{SIDECAR_FILE} is missing, unreadable or holds different data")
    edits: list[tuple[int, int, list[str]]] = [(first, last, [])]
    write = None
    if not declared and not dropped:
        write = None if existing is not None else _sidecar_text(lines[first:last], block)
        if "metadata" in meta:
            placed = _pointer_lines(lines, meta)
            if isinstance(placed, str):
                return _refusal(text, placed)
            edits.append((placed[0], placed[0], placed[1]))
        else:
            pointer = [f"  {POINTER_KEY}: {SIDECAR_FILE}\n", f"  {FORMAT_KEY}: '{SIDECAR_FORMAT}'\n"]
            edits[0] = (first, last, ["metadata:\n", *pointer])
    for edit_start, edit_end, new in sorted(edits, key=lambda edit: edit[:2], reverse=True):
        lines[edit_start:edit_end] = new
    return Conversion(
        text[:start] + "".join(lines) + text[end:], write, None if dropped else block,
    )


def _residual(meta: dict[str, Any]) -> dict[str, Any]:
    """Frontmatter without the block and the pointer keys: what must survive unchanged."""
    kept = {key: value for key, value in meta.items() if key != FRONTMATTER_KEY}
    metadata = kept.get("metadata")
    if isinstance(metadata, dict):
        kept["metadata"] = {k: v for k, v in metadata.items() if k not in _POINTER_KEYS}
        if not kept["metadata"]:
            del kept["metadata"]
    return kept


def _scope_roots(workspace: Path, db, unresolved: list[str]) -> list[Path]:
    """The workspace root and every project root the registry resolves from the database."""
    roots = [resolve_workspace_root(workspace)]
    for row in db.execute("SELECT id FROM projects ORDER BY id").fetchall():
        try:
            root = project_dir(str(row[0]), workspace, db)
        except (OSError, ValueError) as exc:
            logger.warning("skill data conversion skipped project_id={}: {}", row[0], exc)
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
            logger.warning("skill data conversion skipped role {} under {}: {}", role, scope_root, exc)
            unresolved.append(f"role {role} under {scope_root}")
    threads = scope_root / THREADS_DIR
    if threads.is_dir() and not threads.is_symlink():
        roots.extend(sorted(tray for tray in threads.iterdir() if tray.is_dir() and not tray.is_symlink()))
    return roots


def _replace(path: Path, expected: bytes | None, raw: bytes, mode: int) -> bool:
    """Atomically swap in ``raw``; False when the file is not as ``expected`` (None: absent)."""
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(raw)
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, mode)
        current = path.read_bytes() if path.exists() or path.is_symlink() else None
        if current != expected:
            return False
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _refuse(path: Path, reason: str) -> Outcome:
    logger.warning("skill data conversion refused path={}: {}", path, reason)
    return "refused"


def _read_back(path: Path, before: dict[str, Any], conversion: Conversion) -> bool:
    """True when the reader resolves the converted skill's data and every other field as before."""
    after = validate_skill_file(path)
    return after.corsia == conversion.data and _residual(after.metadata) == _residual(before)


def _convert_file(path: Path) -> Outcome:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _refuse(path, f"unreadable: {exc}")
    sidecar, problem = read_sidecar(path.parent)
    conversion = rewrite_skill_data(text, sidecar)
    if conversion.refused is None and problem is not None and conversion.text != text:
        return _refuse(path, problem)
    if conversion.refused is not None:
        return _refuse(path, conversion.refused)
    if conversion.text == text:
        return "unchanged"
    if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
        return _refuse(path, f"symlinked skill; convert {path.resolve()} where it lives")
    mode = stat.S_IMODE(path.stat().st_mode)
    sidecar_path = path.parent / SIDECAR_FILE
    try:
        if conversion.sidecar is not None and not _replace(
            sidecar_path, None, conversion.sidecar.encode("utf-8"), mode,
        ):
            return _refuse(path, f"{SIDECAR_FILE} appeared while converting")
        converted = conversion.text.encode("utf-8")
        if not _replace(path, raw, converted, mode):
            return _refuse(path, "changed while converting")
        if _read_back(path, parse_frontmatter(text)[0], conversion):
            return "converted"
        _replace(path, converted, raw, mode)
        if conversion.sidecar is not None:
            sidecar_path.unlink(missing_ok=True)
        return _refuse(path, "the converted skill does not read back as the same data")
    except OSError as exc:
        return _refuse(path, f"not written: {exc}")


def convert_legacy_skill_data(workspace: Path, *, db=None) -> SkillDataConversionResult | None:
    """Move every retired ``corsia:`` block into a sidecar once; None once it has run."""
    if db is None:
        with closing(get_db()) as connection:
            return convert_legacy_skill_data(workspace, db=connection)
    if get_setting(COMPLETED_SETTING, db=db) is not None:
        return None
    counts: dict[Outcome, int] = {"converted": 0, "unchanged": 0, "refused": 0}
    unresolved: list[str] = []
    for scope_root in _scope_roots(workspace, db, unresolved):
        for root in _skill_roots(scope_root, unresolved):
            for path in skill_files(root):
                counts[_convert_file(path)] += 1
    result = SkillDataConversionResult(
        converted=counts["converted"], refused=counts["refused"], unresolved_roots=len(unresolved)
    )
    if unresolved:
        logger.warning(
            "skill data conversion incomplete, retrying next start: converted={} unresolved={}",
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
        "skill data conversion completed: converted={} refused={}", result.converted, result.refused,
    )
    return result
