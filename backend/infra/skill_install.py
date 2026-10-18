"""Install a validated external skill bundle through workspace guidance history."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Mapping

import yaml
from loguru import logger

from core.skill_sidecar import FORMAT_KEY, POINTER_KEY, SIDECAR_FILE, SIDECAR_FORMAT
from core.skill_validation import parse_frontmatter, validate_skill_bundle
from infra.guidance_git import GuidanceTarget, save_guidance
from infra.guidance_messages import saver_author

ALREADY_HERE = "already_here"
INSTALLED = "installed"
FAILED = "failed"
_TOOL_GRANTS = frozenset({"allowed-tools", "hooks"})


def _nested(value: object) -> bool:
    return isinstance(value, dict) or (isinstance(value, list) and any(isinstance(item, dict) for item in value))


def _skill_text(name: str, upstream: bytes, description: str) -> str | None:
    """Return an install-safe format-1 SKILL.md, or None for invalid upstream text."""
    try:
        metadata, body = parse_frontmatter(upstream.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    source_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    safe_metadata = {
        key: value for key, value in source_metadata.items()
        if key not in _TOOL_GRANTS | {POINTER_KEY, FORMAT_KEY} and not _nested(value)
    }
    safe_metadata.update({POINTER_KEY: SIDECAR_FILE, FORMAT_KEY: SIDECAR_FORMAT})
    front = {
        "name": name,
        "description": description,
        **{key: metadata[key] for key in ("license", "compatibility") if key in metadata},
        "metadata": safe_metadata,
    }
    return "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n" + body


def _sidecar(origin: Mapping[str, str]) -> str:
    """The installed sidecar holds only the import origin; an upstream one could carry a handler."""
    return yaml.safe_dump({"origin": dict(origin)}, sort_keys=False, allow_unicode=True)


def _holds(path: Path, data: bytes) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.read_bytes() == data
    except OSError:
        return False


def _discard(target: Path, name: str) -> str:
    """Remove a bundle this install left incomplete, so a re-run can offer it again."""
    logger.warning("skill install failed at guidance save for {}", name)
    shutil.rmtree(target, ignore_errors=True)
    return FAILED


def install_skill_bundle(
    workspace: Path,
    name: str,
    upstream_skill_md: bytes,
    other_files: Mapping[str, bytes],
    description: str,
    origin: Mapping[str, str],
    *,
    author_thread_id: str,
    consent_decision_id: str | None = None,
) -> str:
    """Install one imported skill with no overwrite and a single guidance save."""
    skill_md = _skill_text(name, upstream_skill_md, description)
    if skill_md is None:
        logger.warning("skill install failed at frontmatter rewrite for {}", name)
        return FAILED
    sidecar = _sidecar(origin)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("skill install failed at workspace setup for {}", name)
        return FAILED
    skills = workspace / "skills"
    target = skills / name
    if not name or Path(name).name != name or skills.is_symlink():
        logger.warning("skill install failed at destination validation for {}", name)
        return FAILED
    if target.exists() or target.is_symlink():
        return ALREADY_HERE
    files = {"SKILL.md": skill_md.encode("utf-8"), SIDECAR_FILE: sidecar.encode("utf-8")}
    for relative, data in other_files.items():
        if relative == SIDECAR_FILE:
            continue
        path = Path(relative)
        if relative in files or path.is_absolute() or ".." in path.parts:
            logger.warning("skill install failed at bundle path validation for {}", name)
            return FAILED
        files[relative] = data
    writes = {target / relative: data for relative, data in files.items()}
    if not validate_skill_bundle(skill_md, {SIDECAR_FILE: sidecar}).ok:
        logger.warning("skill install failed at bundle validation for {}", name)
        return FAILED
    target_info = GuidanceTarget("skill, workspace scope")
    try:
        saved = save_guidance(
            writes,
            workspace=workspace,
            target=target_info,
            author=saver_author(thread_id=author_thread_id),
            reason="consented skill import from agent_import",
            evidence=consent_decision_id,
            expected={path: None for path in writes},
        )
    except OSError:
        return _discard(target, name)
    if saved.outcome == "stale":
        return ALREADY_HERE
    # A save without history still writes every file; what is on disk decides the outcome.
    if not all(_holds(path, data) for path, data in writes.items()):
        return _discard(target, name)
    if saved.outcome not in {"written", "unchanged"}:
        logger.info("skill {} installed without guidance history: {}", name, saved.outcome)
    return INSTALLED


def origin_metadata(source: str, upstream_skill_md: bytes, adopted_at: str) -> dict[str, str]:
    """Create the stable origin payload stored beside an imported skill."""
    return {
        "source": source,
        "sha256": hashlib.sha256(upstream_skill_md).hexdigest(),
        "adopted_at": adopted_at,
    }
