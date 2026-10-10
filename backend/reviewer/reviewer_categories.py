"""Finding category taxonomy for the LLM code reviewer."""

from __future__ import annotations

from typing import Any

SEVERITY_BLOCK = "block"
SEVERITY_FLAG = "flag"
SEVERITY_NIT = "nit"
APPROACH_CHURN_CATEGORY = "review-process/approach-churn"


def is_approach_reset_finding(finding: dict[str, Any]) -> bool:
    """Whether one finding requires the author to stop and reset approach."""
    return (
        finding.get("category") == APPROACH_CHURN_CATEGORY
        and finding.get("severity") == SEVERITY_BLOCK
    )


VALID_SEVERITIES = frozenset({SEVERITY_BLOCK, SEVERITY_FLAG, SEVERITY_NIT})

VALID_CATEGORIES: tuple[str, ...] = (
    "anti-pattern/memorializing",
    "anti-pattern/reactive-guard",
    "anti-pattern/no-search-before-create",
    "anti-pattern/hardcoded-model-name",
    "anti-pattern/multi-branch-anchor",
    "anti-pattern/annotate-drift",
    "anti-pattern/hardcoded-user-name",
    "anti-pattern/memorial-kpi",
    "architecture/subsystem-conflict",
    "architecture/dependency-cycle",
    # No architecture/loc-budget: the file-size ratchet measures LOC budgets
    # deterministically and blocks on its own check. A second, unauthoritative
    # LLM opinion on the same rule added no enforcement and 8 of 90 recent
    # gate-blocking flags.
    "architecture/prompt-in-logic",
    "test-gap/missing-unit",
    "test-gap/missing-integration",
    "test-gap/missing-edge-case",
    "naming/convention",
    "naming/misleading",
    "security/secret-exposure",
    "security/path-traversal",
    "security/injection",
    "security/auth-bypass",
    "security/permission-boundary",
    "performance/n-plus-one",
    "performance/blocking-in-async",
    "performance/memory-pressure",
    "performance/cache-prefix-break",
    "correctness/logic-error",
    "correctness/race-condition",
    "correctness/silent-error",
    "style/readability",
    "style/dead-code",
    APPROACH_CHURN_CATEGORY,
    # Standards-contract violations — cite the obligation ID (EAS#/PS#/UX#)
    # in ``cited_section``. Block/Flag/Nit reuse the existing severities 1:1.
    "standards/eng-arch",
    "standards/product",
    "standards/ux-ia",
    # PRD-conformance — the diff vs the applicable approved PRD §0.0. Cite the
    # PRD requirement ID (e.g. CONNECTIONS-FR1.1) in ``cited_section``, or the
    # PRD slug + §0.0 heading when no stable ID exists.
    "prd-conformance/missing-requirement",
    "prd-conformance/drift",
    "prd-conformance/scope-creep",
)

VALID_CATEGORIES_SET = frozenset(VALID_CATEGORIES)

LABEL_REVIEW_LOCAL = "qa/review-local"
LABEL_REVIEW_PARALLEL = "qa/review-parallel"
LABEL_OVERRIDE = "qa/override-reviewer"

STATUS_CHECK_CONTEXT = "qa/llm-reviewer"

RETRY_BACKOFF_MINUTES = (1, 2, 4, 8, 16)
MAX_RETRIES = len(RETRY_BACKOFF_MINUTES)
