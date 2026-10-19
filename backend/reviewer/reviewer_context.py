"""Resolve the bounded standards context used by the code reviewer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from loguru import logger


@dataclass
class ReviewerContextManifest:
    """Declare the standards and anti-pattern context for one code review."""

    anti_patterns: bool = False
    standards: list[str] = field(default_factory=list)


_STANDARDS_REGISTRY = {
    # The workspace clean-slate cutover (2026-07-23) moved the standards under
    # shared/standards/; the reviewer must error loudly, not review without
    # its contracts, when these move again.
    "development-doctrine": (
        "projects/corsia/shared/standards/DEVELOPMENT-DOCTRINE.md"
    ),
    "eng-arch": "projects/corsia/shared/standards/PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md",
    "product": "projects/corsia/shared/standards/PRODUCT-STANDARDS.md",
    "copy": "projects/corsia/shared/standards/COPY-STANDARD.md",
    "ux-ia": "projects/corsia/shared/standards/PRD-UX-IA-STANDARDS.md",
    "anti-patterns": "projects/corsia/shared/standards/llm-anti-patterns.md",
}

_REVIEWER_CONTRACT_HEADING = "## Reviewer Contract"


def resolve_reviewer_context(
    manifest: ReviewerContextManifest,
    workspace_root: str | Path,
) -> list[tuple[str, str]]:
    """Resolve selected reviewer contracts and the anti-pattern catalog."""
    return resolve_reviewer_context_from_contents(
        manifest,
        workspace_root,
        reviewer_standard_contents(manifest, workspace_root),
    )


def resolve_reviewer_context_from_contents(
    manifest: ReviewerContextManifest,
    workspace_root: str | Path,
    standards: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Format reviewer context from one raw standards snapshot."""
    workspace = Path(workspace_root)
    results: list[tuple[str, str]] = []

    for key in manifest.standards:
        _add_standard_content(workspace, key, standards.get(key), results)

    if manifest.anti_patterns and (content := standards.get("anti-patterns")) is not None:
        results.append((str(workspace / _STANDARDS_REGISTRY["anti-patterns"]), content))

    return results


def reviewer_context_to_prompt_text(resolved: list[tuple[str, str]]) -> str:
    """Format resolved reviewer files as a delimited prompt block."""
    if not resolved:
        return ""

    parts: list[str] = ["--- BEGIN CONTEXT MANIFEST ---"]
    for path, content in resolved:
        parts.append(f"\n### FILE: {path}\n")
        parts.append(content.rstrip())
        parts.append("")
    parts.append("--- END CONTEXT MANIFEST ---")
    return "\n".join(parts)


def reviewer_standards_registry() -> dict[str, str]:
    """Return a copy of the reviewer standards path map."""
    return dict(_STANDARDS_REGISTRY)


def reviewer_standard_contents(
    manifest: ReviewerContextManifest,
    workspace_root: str | Path,
) -> dict[str, str]:
    """Return the raw bytes-equivalent text of standards loaded for a review."""
    workspace = Path(workspace_root)
    keys = [*manifest.standards]
    if manifest.anti_patterns:
        keys.append("anti-patterns")

    contents: dict[str, str] = {}
    for key in keys:
        relative_path = _STANDARDS_REGISTRY.get(key)
        if relative_path is None:
            continue
        content = _read_file(str(workspace / relative_path))
        if content is not None:
            contents[key] = content
    return contents


def reviewer_standard_contents_for_keys(
    standards: list[str],
    workspace_root: str | Path,
) -> dict[str, str]:
    """Read the raw standard files used by a code-review invocation."""
    return reviewer_standard_contents(
        ReviewerContextManifest(anti_patterns=True, standards=standards),
        workspace_root,
    )


def reviewer_version_digest(
    prompt_text: str,
    standards: Mapping[str, str],
    model: str,
) -> str:
    """Return a stable digest of the prompt, injected standards, and model."""
    payload = {
        "model": model,
        "prompt": prompt_text,
        "standards": {
            key: _reviewer_version_standard_text(key, standards[key])
            for key in sorted(standards)
        },
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def _read_file(path: str) -> str | None:
    """Read one reviewer context file, warning when it is unavailable."""
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            logger.warning(f"Reviewer context: file not found: {path}")
            return None
        return candidate.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Reviewer context: error reading {path}: {exc}")
        return None


def _reviewer_version_standard_text(key: str, content: str) -> str:
    """Return the exact text injected for one standard in a reviewer prompt."""
    return content if key == "anti-patterns" else _extract_reviewer_contract(content)


def _add_standard_content(
    workspace: Path,
    key: str,
    content: str | None,
    results: list[tuple[str, str]],
) -> None:
    """Append one resolved standard's injected Reviewer Contract section."""
    relative_path = _STANDARDS_REGISTRY.get(key)
    if relative_path is None:
        logger.warning(f"Reviewer context: unknown standards key: {key!r}")
        return
    if content is not None:
        results.append(
            (str(workspace / relative_path), _extract_reviewer_contract(content))
        )


def _extract_reviewer_contract(content: str) -> str:
    """Return the H1 title and machine-actionable Reviewer Contract section."""
    lines = content.splitlines()
    title = next((line for line in lines if line.startswith("# ")), "")
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == _REVIEWER_CONTRACT_HEADING
        ),
        None,
    )
    if start is None:
        logger.warning(
            "Reviewer context: no '## Reviewer Contract' section found; "
            "injecting whole standards doc"
        )
        return content

    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    section = "\n".join(lines[start:end]).rstrip()
    return f"{title}\n\n{section}" if title else section
