"""Product-event helpers for the LLM reviewer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def emit_finding_events(
    emit: Callable[..., None],
    findings: list[dict[str, Any]],
    pr_number: int,
    reviewer_version: str,
    reviewer_standards: list[str],
) -> None:
    """Stamp reviewer attribution on each raw finding event."""
    for finding in findings:
        emit(
            "reviewer", "finding.created",
            properties={
                "pr_number": pr_number, "category": finding["category"],
                "severity": finding["severity"], "file": finding["file"],
                "cited_section": finding.get("cited_section"),
                "reviewer_version": reviewer_version,
                "reviewer_standards": reviewer_standards,
            },
        )
