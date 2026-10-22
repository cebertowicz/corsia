"""Citation-check orchestration: extract living-document citations, verify.

Route and ``corsia`` subcommand citations are extracted from the repository
README. Path citations are additionally checked in ``ARCHITECTURE.md``,
``BACKEND.md``, ``FRONTEND.md``, and ``SECURITY.md`` -- the deliverable's
minimum bar for those four ("paths and commands they cite must still
resolve") without inventing route/subcommand extraction for them.
``CHANGELOG.md`` is deliberately excluded from all of this (see the comment
by ``PATH_CHECK_DOCS`` below): it is an append-only historical record whose
past entries cite paths that have since moved or been removed by design.

Version-floor verification covers only the exact-version Python citation
("Python 3.11" vs each CI workflow's pinned ``python-version``), read live
from whichever of ``PRIMARY_DOCS`` states it -- never duplicated as a
hardcoded literal, which would itself be exactly the kind of second source
of truth this tool exists to catch drift in. The docs also cite "Node 20+"
as a minimum, but CI pins a specific Node version that can legitimately be
>= 20 without contradicting a "20+" floor; comparing a minimum-version
citation against a pinned exact version needs semver comparison this script
does not have an authoritative floor source for, so it is deliberately left
unchecked rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import citation_extract as extract
from . import citation_verify as verify
from .discovery import top_level_names

PRIMARY_DOCS = (Path("README.md"),)
# CHANGELOG.md is deliberately excluded here even though the deliverable
# names it: it is an append-only historical record (the same treatment
# `scripts/file_size_limits.py` gives append-only files elsewhere in this
# repo), and most of its path citations are, by construction, paths a past
# entry moved, renamed, or removed -- for example "Workspace moved from
# `backend/workspace/` to top-level `workspace/`". "Must still resolve" is
# the right bar for a living architecture doc; it is the wrong bar for a
# changelog, and there is no reliable date-based line to draw between its
# still-current and superseded citations without inventing one.
PATH_CHECK_DOCS = PRIMARY_DOCS + (
    Path("ARCHITECTURE.md"),
    Path("BACKEND.md"),
    Path("FRONTEND.md"),
    Path("SECURITY.md"),
)


@dataclass
class CitationResult:
    missing_paths: list[str] = field(default_factory=list)
    missing_routes: list[str] = field(default_factory=list)
    missing_subcommands: list[str] = field(default_factory=list)
    python_version_mismatches: list[str] = field(default_factory=list)
    checked_paths: int = 0
    checked_routes: int = 0
    checked_subcommands: int = 0

    @property
    def ok(self) -> bool:
        return not (
            self.missing_paths
            or self.missing_routes
            or self.missing_subcommands
            or self.python_version_mismatches
        )


def check_citations(repo_root: Path) -> CitationResult:
    """Check citations against living docs and the current repo state."""
    result = CitationResult()
    top_level = top_level_names(repo_root)
    _check_paths(repo_root, top_level, result)
    _check_routes_and_subcommands(repo_root, result)
    cited_version = _cited_python_version(repo_root)
    if cited_version is not None:
        result.python_version_mismatches = verify.python_version_mismatches(
            cited_version, repo_root / ".github" / "workflows"
        )
    return result


def _cited_python_version(repo_root: Path) -> str | None:
    """The exact Python version cited by the first of PRIMARY_DOCS that states one."""
    for doc in PRIMARY_DOCS:
        full = repo_root / doc
        if not full.exists():
            continue
        version = extract.extract_cited_python_version(full.read_text(encoding="utf-8"))
        if version is not None:
            return version
    return None


def _check_paths(repo_root: Path, top_level: set[str], result: CitationResult) -> None:
    candidates: set[str] = set()
    for doc in PATH_CHECK_DOCS:
        full = repo_root / doc
        if full.exists():
            text = full.read_text(encoding="utf-8")
            candidates |= extract.extract_path_candidates(text, top_level)
    result.checked_paths = len(candidates)
    result.missing_paths = verify.missing_paths(candidates, repo_root, fallback_dir="backend")


def _check_routes_and_subcommands(repo_root: Path, result: CitationResult) -> None:
    routes: set[str] = set()
    subcommands: set[tuple[str, ...]] = set()
    for doc in PRIMARY_DOCS:
        full = repo_root / doc
        if not full.exists():
            continue
        text = full.read_text(encoding="utf-8")
        routes |= extract.extract_routes(text)
        subcommands |= extract.extract_corsia_subcommands(text)
    result.checked_routes = len(routes)
    result.checked_subcommands = len(subcommands)
    result.missing_routes = verify.missing_routes(routes, repo_root / "backend" / "api")
    result.missing_subcommands = verify.missing_subcommands(
        subcommands, repo_root / "scripts" / "corsia"
    )
