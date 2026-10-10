"""Check 1: starter-kit copies vs their workspace sources.

For every ``copy``-mapped file, both texts are scrub-normalized (see
``scrub.py``) before diffing: a file whose only differences are scrub
substitutions is clean, anything left over is reported as drift.

This check needs the maintainers' workspace on disk. As a release-gate
precondition it is required by default: a runner that cannot find the
workspace must fail visibly, not silently report clean when it verified
nothing (``require_workspace=True``, ``skip_is_failure`` follows it). The
one legitimate exception is an outside contributor's plain clone, which has
no workspace to compare against; pass ``require_workspace=False``
(``--allow-missing-workspace`` on the CLI) for that case.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import scrub
from .private_scan_check import git_user_name
from .starter_kit_mapping import KIT_ROOT_REL, resolve_source


@dataclass
class StarterKitResult:
    clean: list[str] = field(default_factory=list)
    drift: dict[str, list[str]] = field(default_factory=dict)
    kit_native: list[str] = field(default_factory=list)
    adapted: list[str] = field(default_factory=list)
    missing_source: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    skip_is_failure: bool = False

    @property
    def ok(self) -> bool:
        if self.skipped_reason is not None:
            return not self.skip_is_failure
        return not (self.drift or self.missing_source or self.unmapped)


def _real_name_tokens(repo_root: Path) -> list[str]:
    """Word tokens of the configured git user.name, for scrub's person pattern.

    Short tokens (initials, etc.) are dropped to avoid matching common short
    words by accident. This only matches the configured name's own tokens
    verbatim -- it does not derive nicknames or shortened forms of a given
    name, since that would need a nickname database this tool has no
    authoritative source for. A nickname used in prose without also being
    part of ``git config user.name`` will not be scrubbed or flagged by
    check 3's owner-identity sub-check either, for the same reason.
    """
    name = git_user_name(repo_root)
    return [tok for tok in name.split() if len(tok) >= 3]


def check_starter_kit(
    repo_root: Path, workspace: Path | None, *, require_workspace: bool = True
) -> StarterKitResult:
    """Run check 1 against every file under the repo's starter kit."""
    result = StarterKitResult()
    if workspace is None:
        result.skipped_reason = (
            "workspace not found; starter-kit-vs-source drift needs the "
            "maintainers' workspace and is skipped outside it"
        )
        result.skip_is_failure = require_workspace
        return result

    person_re = scrub.build_person_pattern(_real_name_tokens(repo_root))
    kit_dir = repo_root / KIT_ROOT_REL
    for kit_file in sorted(p for p in kit_dir.rglob("*") if p.is_file()):
        kit_rel = PurePosixPath(kit_file.relative_to(kit_dir).as_posix())
        _check_one(kit_file, kit_rel, workspace, result, person_re)
    return result


def _check_one(
    kit_file: Path,
    kit_rel: PurePosixPath,
    workspace: Path,
    result: StarterKitResult,
    person_re: re.Pattern[str],
) -> None:
    resolution = resolve_source(kit_rel, workspace)
    rel = kit_rel.as_posix()
    if resolution.kind == "kit_native":
        result.kit_native.append(rel)
        return
    if resolution.kind == "unmapped":
        result.unmapped.append(rel)
        return
    assert resolution.source is not None
    if not resolution.source.exists():
        result.missing_source.append(f"{rel} -> {resolution.source}")
        return
    if resolution.kind == "adapted":
        result.adapted.append(f"{rel} -> {resolution.source}")
        return
    _diff_copy(kit_file, resolution.source, rel, result, person_re)


def _diff_copy(
    kit_file: Path, source: Path, rel: str, result: StarterKitResult, person_re: re.Pattern[str]
) -> None:
    kit_text = scrub.normalize_text(kit_file.read_text(encoding="utf-8"), person_re)
    source_text = scrub.normalize_text(source.read_text(encoding="utf-8"), person_re)
    if kit_text == source_text:
        result.clean.append(rel)
        return
    diff = list(
        difflib.unified_diff(
            source_text.splitlines(),
            kit_text.splitlines(),
            fromfile=f"source:{rel}",
            tofile=f"kit:{rel}",
            lineterm="",
            n=1,
        )
    )
    result.drift[rel] = diff
