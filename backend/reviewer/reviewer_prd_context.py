"""Resolve approved PRD contracts for code-review prompt context."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from infra.product_events import emit_product_event
from reviewer.prd_contract import PRD_DIR, load_approved_contract, prd_filename
from reviewer.prd_map import prds_for_changed_paths
from reviewer.reviewer_subsystems import extract_changed_files_from_diff


def resolve_prd_contracts(
    diff: str,
    pr_number: int,
    repo: str,
    workspace_root: str,
    *,
    emit: Callable = emit_product_event,
) -> list[tuple[str, str]]:
    """Resolve applicable approved PRD contracts and report coverage gaps."""
    changed_files = extract_changed_files_from_diff(diff)
    slugs = prds_for_changed_paths(changed_files, workspace_root)

    out: list[tuple[str, str]] = []
    for slug in slugs:
        contract = load_approved_contract(slug, workspace_root)
        if contract is not None:
            prd_path = str(Path(workspace_root) / PRD_DIR / prd_filename(slug))
            out.append((prd_path, contract))

    if not out:
        emit(
            "reviewer",
            "prd_coverage_gap",
            properties={
                "pr_number": pr_number,
                "repo": repo,
                "mapped_slugs": slugs,
                "changed_file_count": len(changed_files),
            },
        )
    return out
