"""Format the top-level review summary body.

Per-finding bodies live in ``reviewer_threads.format_threaded_comment_body``
(those become line-level review-comment threads, one per finding). This
module renders only the TOP-LEVEL review body — counts + delta summary
across runs. The body is the ``body`` field of the ``POST .../pulls/N/reviews``
call (not an issue comment).
"""

from __future__ import annotations

from typing import Any, Literal

from reviewer.review_thread_lifecycle import StillRaisedFinding
from reviewer.reviewer_categories import (
    SEVERITY_BLOCK,
    SEVERITY_FLAG,
    SEVERITY_NIT,
    is_approach_reset_finding,
)


def _requires_approach_reset(findings: list[dict[str, Any]]) -> bool:
    return any(
        is_approach_reset_finding(finding) for finding in findings
    )


def _review_guidance(
    findings: list[dict[str, Any]], flag_count: int, nit_count: int,
) -> list[str]:
    """Guide authors without turning findings into patch instructions."""
    lines: list[str] = []
    if findings:
        lines.extend([
            "_**Before revising:** A finding may be wrong. Findings are evidence, "
            "not authority or implementation instructions. Verify each against "
            "the current tree before editing._",
            "",
        ])
    if any(
        finding.get("severity") in {SEVERITY_BLOCK, SEVERITY_FLAG}
        for finding in findings
    ):
        lines.extend([
            "_Take one wide-lens pass: look for a shared cause and evaluate any "
            "proposed fix against existing owners, affected callers and sibling "
            "flows, preserved state, and failure paths. A small local fix is fine "
            "when it is the simplest complete answer. Investigate a simpler "
            "approach if local fixes would leave related problems, create new ones, "
            "duplicate an existing concept, or add concepts, state, indirection, "
            "or special-case paths whose cost to understand and maintain is out "
            "of proportion to the problem. If every viable approach is materially "
            "more complex, do not treat the review as authorization to make that "
            "investment; make the tradeoff explicit and proceed only after that "
            "cost is explicitly accepted._",
            "",
        ])
    if flag_count or nit_count:
        lines.extend([
            "_Flags and nits are diagnostic evidence, not patch requests. "
            "Do not revise a clean candidate solely to silence them._",
            "",
        ])
    return lines


def _severity_counts(findings: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return block, flag, and nit totals for a reviewer finding set."""
    return (
        sum(1 for f in findings if f["severity"] == SEVERITY_BLOCK),
        sum(1 for f in findings if f["severity"] == SEVERITY_FLAG),
        sum(1 for f in findings if f["severity"] == SEVERITY_NIT),
    )


def format_status_summary(
    findings: list[dict[str, Any]], model_label: str,
) -> tuple[Literal["none", "approach_reset"], str, int, int, int]:
    """Return the machine action, status text, and visible severity counts."""
    block_count, flag_count, nit_count = _severity_counts(findings)
    required_action: Literal["none", "approach_reset"] = (
        "approach_reset" if _requires_approach_reset(findings) else "none"
    )
    prefix = "STOP: approach reset required -- " if required_action == "approach_reset" else ""
    summary = f"{prefix}{block_count} block, {flag_count} flag, {nit_count} nit -- reviewed by {model_label}"
    return required_action, summary, block_count, flag_count, nit_count


def _summary_header(
    findings: list[dict[str, Any]],
    model_label: str,
    severity_summary: str,
    delta_summary: str,
) -> list[str]:
    if _requires_approach_reset(findings):
        return [
            "## :no_entry_sign: STOP — APPROACH RESET REQUIRED",
            "",
            "**Do not patch the findings one-by-one. They are evidence, not "
            "authority; regroup around the shared cause before revising.**",
            "",
            f"**LLM Code Review:** {severity_summary}",
            "",
            f"_Reviewed by {model_label} · {delta_summary}_",
            "",
        ]
    return [
        f"## :mag: LLM Code Review — {severity_summary}",
        "",
        f"_Reviewed by {model_label} · {delta_summary}_",
        "",
    ]


def _still_raised_line(still_raised: StillRaisedFinding) -> str:
    """Render one current-severity pointer to a carried reviewer comment."""
    finding = still_raised.finding
    carrier = still_raised.carrier_comment
    label = {
        SEVERITY_BLOCK: "still blocking",
        SEVERITY_FLAG: "still flagged",
        SEVERITY_NIT: "still a nit",
    }.get(finding.get("severity"), "still raised")
    path = finding.get("file") or "(PR-level)"
    line = finding.get("line")
    location = f"{path}:{line}" if line else path
    html_url = carrier.get("html_url")
    if isinstance(html_url, str) and html_url:
        location = f"[{location}]({html_url})"
    return f"- {label}: {location}"


def format_summary_body(
    findings: list[dict[str, Any]],
    model_label: str,
    new_count: int,
    still_raised: list[StillRaisedFinding],
    resolved_count: int,
    pr_level_count: int,
) -> str:
    """Build the top-level review body.

    Carries severity totals and the cross-run delta. Per-finding detail lives
    in the line-level comments posted alongside.

    Args:
        findings: All findings from the current run.
        model_label: Human model name (e.g., "Opus 4.8").
        new_count: Findings whose fingerprint is new in this run.
        still_raised: Findings whose fingerprint is unchanged from a prior
            run, paired with the existing comment that carries their text.
        resolved_count: Prior findings absent from this run (resolved
            replies posted on those threads).
        pr_level_count: New findings that could not be anchored and were
            published as PR-level comments instead. Stated explicitly so the
            reader is never told to look for a line comment that isn't there.

    """
    block_count, flag_count, nit_count = _severity_counts(findings)
    severity_summary = f"{block_count} block, {flag_count} flag, {nit_count} nit"
    unchanged_count = len(still_raised)
    delta_summary = f"{new_count} new this run · {unchanged_count} still raised this run · {resolved_count} not raised this run"

    lines = _summary_header(
        findings, model_label, severity_summary, delta_summary,
    )

    if not findings and resolved_count == 0:
        lines.append(":white_check_mark: No issues found.")
        return "\n".join(lines)

    if new_count == 0 and unchanged_count == 0 and resolved_count > 0:
        lines.append(
            f":information_source: {resolved_count} prior finding"
            f"{'s' if resolved_count != 1 else ''} not raised this run; "
            "see the finding notes for citation checks."
        )
        return "\n".join(lines)

    if new_count > 0:
        lines.append(
            f"**{new_count} new finding{'s' if new_count != 1 else ''}** "
            f"posted as line-level comment{'s' if new_count != 1 else ''} "
            "below."
        )
        lines.append("")

    lines.extend(_review_guidance(findings, flag_count, nit_count))

    if pr_level_count > 0:
        lines.append(
            f":pushpin: _{pr_level_count} of those could not be anchored to a "
            f"line in this diff and "
            f"{'was' if pr_level_count == 1 else 'were'} posted as a separate "
            f"PR-level comment instead._"
        )
        lines.append("")

    if still_raised:
        lines.extend(_still_raised_line(finding) for finding in still_raised)
        lines.append("")

    if resolved_count > 0:
        lines.append(
            f":information_source: _{resolved_count} prior finding"
            f"{'s' if resolved_count != 1 else ''} not raised this run; "
            "see the finding notes for citation checks._"
        )

    return "\n".join(lines)
