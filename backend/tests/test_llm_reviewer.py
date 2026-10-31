"""Prompt and summary formatting tests for the LLM reviewer."""

from __future__ import annotations

import re
from pathlib import Path


class TestReviewerPrompt:
    """Tests for reviewer_prompt.build_reviewer_prompt."""

    def test_prompt_contains_diff(self):
        """Prompt includes the provided diff text."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(
            diff="--- a/foo.py\n+++ b/foo.py\n+print('hello')",
            context_text="",
            model_label="Test Model",
        )
        assert "+print('hello')" in prompt

    def test_prompt_contains_context(self):
        """Prompt includes the injected context text."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(
            diff="",
            context_text="### FILE: prds/PRD-QUALITY-REVIEW-LIFECYCLE.md\nSome contract content",
            model_label="Review Model",
        )
        assert "PRD-QUALITY-REVIEW-LIFECYCLE.md" in prompt

    def test_prompt_claims_only_current_reviewer_context_sources(self):
        """Prompt source claims match the context resolver's bounded inputs."""
        from prompts.reviewer import build_reviewer_prompt

        prompt = build_reviewer_prompt(diff="", context_text="")

        assert "anti-pattern catalog" in prompt
        assert "Development Doctrine" in prompt
        assert "standards' Reviewer Contracts" in prompt
        assert "approved PRD" in prompt
        assert "recent decisions" not in prompt

    def test_prompt_routes_copy_findings_to_their_own_category(self):
        """The injected copy contract has an instruction and a category to file under."""
        from prompts.reviewer import build_reviewer_prompt

        prompt = build_reviewer_prompt(diff="", context_text="")

        assert "``standards/copy`` for C#" in prompt
        assert "**Copy (always):**" in prompt

    def test_prompt_contains_all_categories(self):
        """Prompt includes every category from the taxonomy."""
        from reviewer.reviewer_categories import VALID_CATEGORIES
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(diff="", context_text="")
        for cat in VALID_CATEGORIES:
            assert cat in prompt, f"Category {cat!r} not found in prompt"

    def test_prompt_contains_severity_levels(self):
        """Prompt mentions all severity levels."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(diff="", context_text="")
        assert "block" in prompt
        assert "flag" in prompt
        assert "nit" in prompt

    def test_prompt_contains_model_label(self):
        """Prompt includes the model label for self-identification."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(
            diff="", context_text="", model_label="qwen3.6-27B Dense",
        )
        assert "qwen3.6-27B Dense" in prompt

    def test_prompt_requests_json_output(self):
        """Prompt instructs the reviewer to output JSON."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(diff="", context_text="")
        assert "JSON array" in prompt

    def test_prompt_keeps_exploit_steps_out_of_security_findings(self):
        """Published findings name the weakness, never a route to exploit it."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(diff="", context_text="")
        assert "never write step-by-step exploitation" in prompt
        assert "credential-capture routes" in prompt

    def test_prompt_reports_defects_without_designing_repairs(self):
        """Review-process policy comes from the injected canonical contract."""
        from prompts.reviewer import build_reviewer_prompt

        contract = (
            "REVIEW-PROCESS-SENTINEL: diagnose only; state defect, evidence, "
            "and consequence; never design repairs."
        )
        prompt = build_reviewer_prompt(diff="", context_text=contract)

        assert contract in prompt
        assert "Reviewer Contract's scope and review-process discipline" in prompt
        assert "wrapper, adapter, registry, cache, fallback" not in prompt

    def test_prompt_searches_for_conceptual_duplication(self):
        """A claimed missing capability is checked against the exact tree first."""
        from prompts.reviewer import build_reviewer_prompt

        contract = "CONCEPTUAL-DUPLICATION-SENTINEL"
        prompt = build_reviewer_prompt(diff="", context_text=contract)

        assert contract in prompt
        assert "exact-tree duplication checks" in prompt

    def test_prompt_aggregates_approach_churn_in_the_same_pass(self):
        """Correlated findings stop local patch backfire without waiting for a cap."""
        from prompts.reviewer import build_reviewer_prompt

        contract = "APPROACH-CHURN-SENTINEL"
        prompt = build_reviewer_prompt(
            diff="", context_text=contract,
            review_history="- BLOCK [standards/eng-arch] backend/old_fix.py",
        )

        assert contract in prompt
        assert "same-pass aggregate approach-churn detection" in prompt
        assert "backend/old_fix.py" in prompt

    def test_exact_source_replaces_the_embedded_diff(self):
        """Tool-enabled reviewers receive commands, not the cumulative diff blob."""
        from prompts.reviewer import build_reviewer_prompt

        prompt = build_reviewer_prompt(
            diff="FROZEN-DIFF-MUST-NOT-APPEAR",
            context_text="",
            change_source="Run git diff BASE...HEAD from the exact object store.",
        )

        assert "Run git diff BASE...HEAD" in prompt
        assert "FROZEN-DIFF-MUST-NOT-APPEAR" not in prompt

    def test_prompt_applies_canonical_doctrine_without_size_bias(self):
        """Reviewer uses the injected doctrine rather than a second local rubric."""
        from prompts.reviewer import build_reviewer_prompt

        prompt = build_reviewer_prompt(diff="", context_text="")

        assert "Development Doctrine as the canonical source" in prompt
        assert "using the severity definitions above" in prompt
        assert "root cause and maintainability" in prompt
        assert "diff size or line count as a quality proxy" in prompt
        assert "fifth mechanism" not in prompt

    def test_prompt_does_not_duplicate_injected_standard_rules(self):
        """The prompt routes standards review without freezing example rules."""
        from prompts.reviewer import build_reviewer_prompt

        prompt = build_reviewer_prompt(
            diff="",
            context_text="INJECTED-STANDARD-SENTINEL EAS42",
        )
        prompt_without_context = prompt.replace(
            "INJECTED-STANDARD-SENTINEL EAS42", "",
        )

        assert "INJECTED-STANDARD-SENTINEL EAS42" in prompt
        assert "walk each injected Reviewer Contract" in prompt
        assert "cite its obligation ID" in prompt
        assert re.findall(r"\b(?:EAS|PS)\d+\b", prompt_without_context) == []


class TestPullRequestTemplate:
    """Tests for the author evidence carried into reviewer context."""

    def test_template_links_code_ground_without_transcribing_it(self):
        template = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "pull_request_template.md"
        ).read_text(encoding="utf-8")

        assert "## First-pass evidence" in template
        assert "Work object containing current author-time evidence" in template
        assert "Relevant tests/build/browser proof" in template
        assert "Fresh-eyes refutation" in template
        assert "Existing owner / reused path" not in template
        assert "Affected callers / siblings / contracts" not in template


class TestSummaryBody:
    """Tests for reviewer_comment.format_summary_body (top-level review body)."""

    def test_empty_findings_no_resolved(self):
        """No findings + no resolves = clean pass message."""
        from reviewer.reviewer_comment import format_summary_body
        body = format_summary_body([], "Claude Fable 5", 0, [], 0, 0)
        assert "No issues found" in body
        assert "0 block, 0 flag, 0 nit" in body

    def test_not_raised_findings_do_not_claim_resolution(self):
        """An omitted finding is reported honestly, not as a verified fix."""
        from reviewer.reviewer_comment import format_summary_body
        body = format_summary_body([], "Claude Fable 5", 0, [], 3, 0)
        assert "not raised this run" in body
        assert "resolved" not in body.lower()
        assert "3" in body

    def test_summary_counts_severities(self):
        """Summary line accurately counts severities."""
        from reviewer.reviewer_comment import format_summary_body
        findings = [
            {"severity": "block", "file": "a.py", "line": 1, "category": "x", "description": "d", "cited_section": None},
            {"severity": "block", "file": "b.py", "line": 2, "category": "x", "description": "d", "cited_section": None},
            {"severity": "flag", "file": "c.py", "line": 3, "category": "x", "description": "d", "cited_section": None},
        ]
        body = format_summary_body(findings, "Claude Fable 5", 3, [], 0, 0)
        assert body.splitlines()[0] == (
            "## :mag: LLM Code Review — 2 block, 1 flag, 0 nit"
        )

    def test_delta_counts_in_summary(self):
        """Body distinguishes this run from findings carried across runs."""
        from reviewer.review_thread_lifecycle import StillRaisedFinding
        from reviewer.reviewer_comment import format_summary_body

        finding = {"severity": "flag", "file": "a.py", "line": 1}
        still_raised = [
            StillRaisedFinding(finding, {"html_url": "https://example.test/comment"})
            for _ in range(5)
        ]
        body = format_summary_body([], "Claude Fable 5", 2, still_raised, 1, 0)

        assert (
            "2 new this run · 5 still raised this run · 1 not raised this run"
            in body
        )

    def test_still_raised_lines_use_current_severity_and_carrier_links(self):
        from reviewer.review_thread_lifecycle import StillRaisedFinding
        from reviewer.reviewer_comment import format_summary_body

        new = {"severity": "flag", "file": "new.py", "line": 5}
        block = {"severity": "block", "file": "backend/x.py", "line": 18}
        flag = {"severity": "flag", "file": "backend/y.py", "line": 19}
        nit = {"severity": "nit", "file": "backend/z.py", "line": 20}
        still_raised = [
            StillRaisedFinding(block, {"html_url": "https://example.test/block"}),
            StillRaisedFinding(flag, {"html_url": "https://example.test/flag"}),
            StillRaisedFinding(nit, {}),
        ]

        body = format_summary_body(
            [new, block, flag, nit], "Reviewer", 1, still_raised, 0, 0,
        )

        assert "**1 new finding**" in body
        assert "- still blocking: [backend/x.py:18](https://example.test/block)" in body
        assert "- still flagged: [backend/y.py:19](https://example.test/flag)" in body
        assert "- still a nit: backend/z.py:20" in body
        assert "still apply — see existing threads" not in body

    def test_model_label_in_body(self):
        """Model label appears in the body."""
        from reviewer.reviewer_comment import format_summary_body
        body = format_summary_body([], "qwen3.6-27B Dense", 0, [], 0, 0)
        assert "qwen3.6-27B Dense" in body

    def test_new_findings_mention_threads(self):
        """Body tells reader the new findings are in line-level threads below."""
        from reviewer.reviewer_comment import format_summary_body
        findings = [
            {"severity": "flag", "file": "a.py", "line": 1, "category": "x", "description": "d", "cited_section": None},
        ]
        body = format_summary_body(findings, "Claude Fable 5", 1, [], 0, 0)
        assert "line-level comment" in body

    def test_pr_level_fallback_is_declared_in_the_summary(self):
        """A reader told to look below must not be sent to a thread that isn't there."""
        from reviewer.reviewer_comment import format_summary_body
        findings = [
            {"severity": "flag", "file": "", "line": 0, "category": "x", "description": "d", "cited_section": None},
        ]
        body = format_summary_body(findings, "Claude Fable 5", 1, [], 0, 1)
        assert "could not be anchored" in body
        assert "PR-level comment" in body

    def test_nits_are_not_patch_requests_or_approach_resets(self):
        from reviewer.reviewer_comment import format_summary_body

        findings = [
            {"severity": "nit", "file": "a.py", "line": 1,
             "category": "standards/eng-arch", "description": "d",
             "cited_section": "EAS3"},
        ]

        body = format_summary_body(findings, "Reviewer", 1, [], 0, 0)

        assert "Flags and nits are diagnostic evidence, not patch requests" in body
        assert "A finding may be wrong" in body
        assert (
            "Findings are evidence, not authority or implementation instructions"
            in body
        )
        assert "Verify each against the current tree before editing" in body
        assert "Take one wide-lens pass" not in body

    def test_actionable_guidance_preserves_small_fixes_and_checks_wider_shape(self):
        """Blocks and flags prompt analysis without mechanically demanding a rewrite."""
        from reviewer.reviewer_comment import format_summary_body

        bodies = [
            format_summary_body(
                [{"severity": severity, "file": "a.py", "line": 1,
                  "category": "correctness/logic", "description": "d",
                  "cited_section": None}],
                "Reviewer", 1, [], 0, 0,
            )
            for severity in ("block", "flag")
        ]

        assert all("Take one wide-lens pass" in body for body in bodies)
        body = bodies[0]
        assert "shared cause" in body
        assert "affected callers and sibling flows" in body
        assert (
            "A small local fix is fine when it is the simplest complete answer"
            in body
        )
        assert "duplicate an existing concept" in body
        assert (
            "add concepts, state, indirection, or special-case paths whose cost "
            "to understand and maintain is out of proportion to the problem"
            in body
        )
        assert (
            "do not treat the review as authorization to make that investment; "
            "make the tradeoff explicit and proceed only after that cost is "
            "explicitly accepted"
            in body
        )

    def test_approach_churn_summary_stops_individual_patching(self):
        from reviewer.reviewer_comment import format_summary_body

        findings = [
            {"severity": "block", "file": "", "line": 0,
             "category": "review-process/approach-churn", "description": "d",
             "cited_section": None},
            {"severity": "block", "file": "a.py", "line": 1,
             "category": "correctness/logic", "description": "d",
             "cited_section": None},
            {"severity": "flag", "file": "b.py", "line": 2,
             "category": "correctness/logic", "description": "d",
             "cited_section": None},
        ]

        body = format_summary_body(findings, "Reviewer", 1, [], 0, 1)

        assert "Do not patch the findings one-by-one" in body
        assert body.splitlines()[0] == (
            "## :no_entry_sign: STOP — APPROACH RESET REQUIRED"
        )
        assert "APPROACH RESET REQUIRED" in "\n".join(body.splitlines()[:4])
        assert body.splitlines()[4] == "**LLM Code Review:** 2 block, 1 flag, 0 nit"


class TestReviewerCategoryOwnership:
    """A deterministic gate owns its rule; the reviewer must not restate it."""

    def test_loc_budget_is_not_an_llm_category(self):
        """The file-size ratchet measures LOC budgets and blocks on its own.

        A second, unauthoritative LLM opinion on the same rule enforced
        nothing and produced 8 of 90 gate-blocking flags measured across
        recent PRs -- including one finding on PR #1190 that reopened in
        eight consecutive rounds.
        """
        from reviewer.reviewer_categories import VALID_CATEGORIES_SET
        assert "architecture/loc-budget" not in VALID_CATEGORIES_SET

    def test_loc_budget_is_absent_from_the_rendered_prompt(self):
        """The prompt renders from the same tuple, so the model stops seeing it."""
        from prompts.reviewer import build_reviewer_prompt
        prompt = build_reviewer_prompt(
            diff="", context_text="", model_label="Test Model",
        )
        assert "architecture/loc-budget" not in prompt

    def test_an_unknown_category_is_kept_not_dropped(self):
        """A model that still emits a retired category must not lose the finding."""
        import json
        from reviewer.reviewer_parser import parse_findings
        raw = json.dumps([{
            "severity": "flag",
            "category": "architecture/loc-budget",
            "file": "backend/x.py",
            "line": 1,
            "description": "d",
        }])
        findings = parse_findings(raw)
        assert len(findings) == 1
        assert findings[0]["category"] == "architecture/loc-budget"

    def test_approach_churn_is_always_a_block(self):
        """An aggregate reset cannot publish a contradictory green status."""
        import json
        from reviewer.reviewer_parser import parse_findings

        raw = json.dumps([{
            "severity": "flag",
            "category": "review-process/approach-churn",
            "file": "",
            "line": 0,
            "description": "The local patch strategy is backfiring.",
        }])

        assert parse_findings(raw)[0]["severity"] == "block"
