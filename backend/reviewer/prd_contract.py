"""Load an APPROVED PRD's contract for the reviewer (stage-2b).

PRDs are the post-spec source of truth (spec-gate retired, #555). The reviewer
judges a diff against the **approved** §0.0 requirements of the PRD(s) that own
the changed paths, plus the PRD's Reviewer Contract if present. A draft PRD
(``§0.0 Approved`` unticked) is neither injected nor enforceable, and the whole
PRD is never injected — only the contract-bearing sections.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

# PRDs are workspace-canonical. This is the one place that knows the slug→file
# convention — when the ``PRD-`` prefix drops, change it here only.
# The workspace clean-slate cutover (2026-07-23) moved PRDs under
# shared/planning/ (standards live in shared/standards/ — see
# reviewer_context._STANDARDS_REGISTRY).
PRD_DIR = "projects/corsia/shared/planning"


class PRDContractError(RuntimeError):
    """An APPROVED PRD's contract could not be extracted.

    Distinct from a *coverage gap* ("no PRD owns these paths"): here an approved
    PRD exists but its ``§0.0`` / ``Reviewer Contract`` sections could not be
    parsed, so the reviewer would otherwise run WITHOUT a contract it is meant
    to enforce. The reviewer must fail loud (red, retryable via the runner's
    error path) rather than silently downgrade a broken approved contract to a
    coverage gap — conflating the two is the silent-error this guards against.
    """


def prd_filename(slug: str) -> str:
    """Map a PRD slug to its canonical filename (e.g. ``event-delivery``)."""
    return f"PRD-{slug.upper()}.md"


def load_approved_contract(slug: str, workspace_root: str | Path) -> str | None:
    """Return an approved PRD's contract text, or None if not enforceable.

    None when: the PRD file is missing, or its ``§0.0 Approved`` checkbox is
    unticked (draft) — both are legitimate "no enforceable contract" states the
    caller treats as a coverage gap. Otherwise returns the H1 title + the §0.0
    Requirements section + the Reviewer Contract section (if the PRD has one).

    Raises ``PRDContractError`` when the PRD IS approved but no §0.0 / Reviewer
    Contract section can be extracted: an approved-but-unparseable contract must
    fail the review loud (red, retryable), never silently degrade to a gap.
    """
    path = Path(workspace_root) / PRD_DIR / prd_filename(slug)
    if not path.is_file():
        logger.info(f"PRD contract: no PRD file for slug {slug!r} at {path}")
        return None

    content = path.read_text(encoding="utf-8")
    if not _is_approved(content):
        logger.info(f"PRD contract: {slug!r} §0.0 not approved — not enforceable")
        return None

    lines = content.splitlines()
    sections = [
        sec for sec in (
            _extract_section(lines, _heading_index(lines, "§0.0")),
            _extract_section(lines, _heading_index(lines, "Reviewer Contract")),
        ) if sec
    ]
    if not sections:
        raise PRDContractError(
            f"PRD contract: {slug!r} is approved but no §0.0 / Reviewer Contract "
            "section could be extracted (check the headings) — refusing to review "
            "without the contract this approved PRD is meant to enforce"
        )

    title = next((ln for ln in lines if ln.startswith("# ")), "")
    parts = ([title] if title else []) + sections
    return "\n\n".join(parts)


def _is_approved(content: str) -> bool:
    """True when the PRD's ``§0.0 Approved`` checkbox is ticked.

    Accepts both ``[x]`` and ``[X]`` — GitHub/Markdown render either as checked,
    so a maintainer using the uppercase form must not be silently ignored.
    """
    return any(
        "[x]" in ln.lower() and "0.0 approved" in ln.lower()
        for ln in content.splitlines()
    )


def _heading_index(lines: list[str], needle: str) -> int | None:
    """Index of the first ``## `` heading containing ``needle``, else None."""
    return next(
        (i for i, ln in enumerate(lines) if ln.startswith("## ") and needle in ln),
        None,
    )


def _extract_section(lines: list[str], start: int | None) -> str:
    """Return a ``## `` section (heading → next ``## ``), or '' if start is None."""
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).rstrip()
