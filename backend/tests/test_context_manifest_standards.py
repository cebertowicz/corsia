"""Tests for standards-contract injection into reviewer context (Stage 1).

Covers reviewer-context standards resolution, Reviewer-Contract section
extraction, the new ``standards/*`` finding categories, and the reviewer
``_build_context`` wiring that selects doctrine + Eng/Arch + Product + Copy for
code PRs.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_ENG_ARCH_FIXTURE = """\
# Engineering & Architecture Standards

**The bar:** code a staff engineer passes without a sigh.

## 1 · One path
- **EAS1_BODY.** This body bullet lives outside the Reviewer Contract and must
  NOT appear in the extracted injection.

## Reviewer Contract
- **Block:** a hardcoded value (EAS10) · a silent fallback (EAS11).
- **Flag:** a fork that should've extended (EAS3).
- **Nit:** style, micro-naming.
"""

_PRODUCT_FIXTURE = """\
# Product Standards

**The bar: the product should feel like magic.**

## 1 · Solve the job
- **PS1_BODY.** Body bullet that must not leak into the extracted injection.

## Reviewer Contract
- **Block:** the product lying about state (PS7) · a dark pattern (PS9).
- **Flag:** an avoidable interruption (PS4).
- **Nit:** copy tone.
"""

_DEVELOPMENT_DOCTRINE_FIXTURE = """\
# Development doctrine: maintainability is the test

## The question to ask of every change
**Is this a root-cause fix, and is it the answer that leaves the codebase most
maintainable?**

## Complexity is allowed. It has to pay for itself.
If you cannot name what got simpler, it did not pay for itself.
"""

_COPY_FIXTURE = """\
# CORSIA copy standard

## Scope
- SCOPE_BODY that must not leak into the extracted injection.

## Reviewer Contract
COPY_CONTRACT_INTRO

### Rules
| C1 | RULES_ROW |

### Checked at
CHECKED_AT_BODY

### Severity
SEVERITY_BODY

### Glossary
GLOSSARY_BODY

## Reference
- REFERENCE_BODY after the contract that must be excluded.
"""

_NO_CONTRACT_FIXTURE = """\
# Malformed Standard

## 1 · Something
- A bullet but no Reviewer Contract section at all.
"""

_CONTRACT_THEN_MORE_FIXTURE = """\
# Eng Standard

## 1 · Foo
- EAS1_BODY bullet outside the contract.

## Reviewer Contract
- **Block:** a hardcoded value (EAS10).

## Appendix
- TRAILING_SECTION content after the contract that must be excluded.
"""


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a tmp workspace with standards docs at their registry paths."""
    standards = tmp_path / "projects" / "corsia" / "shared" / "standards"
    standards.mkdir(parents=True)
    for name, content in files.items():
        (standards / name).write_text(content, encoding="utf-8")
    return tmp_path


class TestStandardsResolution:
    def test_subagents_package_does_not_export_reviewer_context(self):
        import subagents

        assert not hasattr(subagents, "ContextManifest")

    def test_single_standard_injects_reviewer_contract(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
            reviewer_context_to_prompt_text,
        )

        ws = _make_workspace(
            tmp_path,
            {"PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": _ENG_ARCH_FIXTURE},
        )
        manifest = ReviewerContextManifest(standards=["eng-arch"])

        resolved = resolve_reviewer_context(manifest, workspace_root=ws)

        std = [c for p, c in resolved if "ENGINEERING-ARCHITECTURE" in p]
        assert len(std) == 1, "eng-arch standard should resolve to exactly one entry"
        content = std[0]
        assert "Engineering & Architecture Standards" in content  # H1 grounding
        assert "Reviewer Contract" in content
        assert "EAS10" in content
        # Section-extracted: the §1 body must NOT be injected.
        assert "EAS1_BODY" not in content

        prompt_text = reviewer_context_to_prompt_text(resolved)
        assert prompt_text.startswith("--- BEGIN CONTEXT MANIFEST ---\n")
        assert prompt_text.endswith("\n--- END CONTEXT MANIFEST ---")

    def test_anti_patterns_inject_from_catalog_registry_path(self, tmp_path):
        """The anti-pattern catalog resolves from its canonical relative path."""
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        catalog = "# LLM anti-pattern catalog\n\n- Never silently drop context.\n"
        ws = _make_workspace(tmp_path, {"llm-anti-patterns.md": catalog})

        resolved = resolve_reviewer_context(
            ReviewerContextManifest(anti_patterns=True),
            workspace_root=ws,
        )

        assert resolved == [
            (
                str(
                    ws
                    / "projects/corsia/shared/standards/llm-anti-patterns.md"
                ),
                catalog,
            )
        ]

    def test_multiple_standards_resolve_in_order(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        ws = _make_workspace(
            tmp_path,
            {
                "DEVELOPMENT-DOCTRINE.md": _DEVELOPMENT_DOCTRINE_FIXTURE,
                "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": _ENG_ARCH_FIXTURE,
                "PRODUCT-STANDARDS.md": _PRODUCT_FIXTURE,
            },
        )
        manifest = ReviewerContextManifest(
            standards=["development-doctrine", "eng-arch", "product"]
        )

        resolved = resolve_reviewer_context(manifest, workspace_root=ws)
        std_paths = [p for p, _ in resolved if "/standards/" in p]

        assert any("DEVELOPMENT-DOCTRINE" in p for p in std_paths)
        assert any("ENGINEERING-ARCHITECTURE" in p for p in std_paths)
        assert any("PRODUCT-STANDARDS" in p for p in std_paths)
        # Manifest order is preserved.
        doctrine_idx = next(i for i, p in enumerate(std_paths) if "DEVELOPMENT" in p)
        eng_idx = next(i for i, p in enumerate(std_paths) if "ENGINEERING" in p)
        prod_idx = next(i for i, p in enumerate(std_paths) if "PRODUCT" in p)
        assert doctrine_idx < eng_idx < prod_idx

    def test_development_doctrine_without_contract_injects_whole_doc(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        ws = _make_workspace(
            tmp_path,
            {"DEVELOPMENT-DOCTRINE.md": _DEVELOPMENT_DOCTRINE_FIXTURE},
        )

        resolved = resolve_reviewer_context(
            ReviewerContextManifest(standards=["development-doctrine"]),
            workspace_root=ws,
        )

        assert resolved == [
            (
                str(
                    ws
                    / "projects/corsia/shared/standards/DEVELOPMENT-DOCTRINE.md"
                ),
                _DEVELOPMENT_DOCTRINE_FIXTURE,
            )
        ]

    def test_unknown_standard_key_skipped(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        ws = _make_workspace(tmp_path, {})
        manifest = ReviewerContextManifest(standards=["not-a-real-standard"])

        resolved = resolve_reviewer_context(manifest, workspace_root=ws)

        assert all("STANDARDS" not in p for p, _ in resolved)

    def test_missing_standard_file_skipped(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        # Known key, but the file does not exist in the workspace.
        ws = _make_workspace(tmp_path, {})
        manifest = ReviewerContextManifest(standards=["eng-arch"])

        resolved = resolve_reviewer_context(manifest, workspace_root=ws)

        assert all("ENGINEERING-ARCHITECTURE" not in p for p, _ in resolved)

    def test_extract_truncates_at_next_section_after_contract(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        ws = _make_workspace(
            tmp_path,
            {"PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": _CONTRACT_THEN_MORE_FIXTURE},
        )
        manifest = ReviewerContextManifest(standards=["eng-arch"])

        resolved = resolve_reviewer_context(manifest, workspace_root=ws)
        content = next(c for p, c in resolved if "ENGINEERING" in p)

        assert "EAS10" in content  # the contract itself
        assert "TRAILING_SECTION" not in content  # section after contract excluded
        assert "EAS1_BODY" not in content  # section before contract excluded

    def test_copy_contract_keeps_subsections_and_stops_at_next_h2(self, tmp_path):
        """Copy's contract spans its ### subsections and ends at ## Reference."""
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        ws = _make_workspace(tmp_path, {"COPY-STANDARD.md": _COPY_FIXTURE})

        resolved = resolve_reviewer_context(
            ReviewerContextManifest(standards=["copy"]), workspace_root=ws
        )
        content = next(c for p, c in resolved if "COPY-STANDARD" in p)

        assert content.startswith("# CORSIA copy standard")
        for kept in (
            "COPY_CONTRACT_INTRO",
            "RULES_ROW",
            "CHECKED_AT_BODY",
            "SEVERITY_BODY",
            "GLOSSARY_BODY",
        ):
            assert kept in content
        assert "SCOPE_BODY" not in content
        assert "REFERENCE_BODY" not in content

    def test_missing_contract_section_falls_back_to_whole_doc(self, tmp_path):
        from reviewer.reviewer_context import (
            ReviewerContextManifest,
            resolve_reviewer_context,
        )

        ws = _make_workspace(
            tmp_path,
            {"PRD-UX-IA-STANDARDS.md": _NO_CONTRACT_FIXTURE},
        )
        manifest = ReviewerContextManifest(standards=["ux-ia"])

        resolved = resolve_reviewer_context(manifest, workspace_root=ws)

        std = [c for p, c in resolved if "UX-IA" in p]
        assert len(std) == 1
        # No Reviewer Contract section → inject the whole doc rather than nothing.
        assert "A bullet but no Reviewer Contract" in std[0]

    def test_manifest_surface_is_standards_and_anti_patterns_only(self):
        from reviewer.reviewer_context import ReviewerContextManifest

        manifest = ReviewerContextManifest(standards=["eng-arch", "product"])

        assert vars(manifest) == {
            "anti_patterns": False,
            "standards": ["eng-arch", "product"],
        }


class TestStandardsCategories:
    @pytest.mark.parametrize(
        "category",
        ["standards/eng-arch", "standards/product", "standards/ux-ia"],
    )
    def test_standards_categories_are_valid(self, category):
        from reviewer.reviewer_categories import VALID_CATEGORIES_SET

        assert category in VALID_CATEGORIES_SET


class TestBuildContextStandards:
    def _ws_with_standards(self, tmp_path):
        return _make_workspace(
            tmp_path,
            {
                "DEVELOPMENT-DOCTRINE.md": _DEVELOPMENT_DOCTRINE_FIXTURE,
                "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": _ENG_ARCH_FIXTURE,
                "PRODUCT-STANDARDS.md": _PRODUCT_FIXTURE,
                "COPY-STANDARD.md": _COPY_FIXTURE,
                "llm-anti-patterns.md": "# LLM anti-pattern catalog\n",
                # exists, but must NOT be injected into the code reviewer
                "PRD-UX-IA-STANDARDS.md": _NO_CONTRACT_FIXTURE,
            },
        )

    def test_code_review_injects_doctrine_eng_arch_product_and_copy_not_ux(
        self, tmp_path, monkeypatch
    ):
        """Code PRs get doctrine + Eng/Arch + Product + Copy; UX/IA stays separate."""
        from reviewer import on_pr_review

        ws = self._ws_with_standards(tmp_path)
        monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(on_pr_review, "_PROJECT_ROOT", str(ws))

        text = on_pr_review._build_context(diff="", pr_number=1, repo="owner/repo")

        assert "DEVELOPMENT-DOCTRINE" in text
        assert "ENGINEERING-ARCHITECTURE" in text
        assert "PRODUCT-STANDARDS" in text
        assert "COPY-STANDARD" in text
        assert "GLOSSARY_BODY" in text
        assert "UX-IA" not in text

    def test_context_manifest_has_no_subsystem_spec_surface(self, monkeypatch):
        """Retired subsystem specs cannot re-enter through a manifest field."""
        from reviewer import on_pr_review
        from reviewer.reviewer_context import reviewer_standards_registry

        captured = {}

        def _spy_resolve(manifest, workspace_root):
            captured["has_project_specs"] = hasattr(manifest, "project_specs")
            reg = reviewer_standards_registry()
            return [
                (f"/ws/{reg[k]}", "## Reviewer Contract\n- Block: x")
                for k in [*on_pr_review._CODE_REVIEW_STANDARDS, "anti-patterns"]
            ]

        monkeypatch.setattr(on_pr_review, "resolve_reviewer_context", _spy_resolve)

        on_pr_review._build_context(
            diff="+++ b/backend/core/foo.py\n@@ +1 @@\n+x = 1\n",
            pr_number=1,
            repo="owner/repo",
        )

        assert captured["has_project_specs"] is False

    def test_missing_required_standard_fails_loud(self, tmp_path, monkeypatch):
        """A missing required standard must error, not silently degrade the gate."""
        from reviewer import on_pr_review

        ws = _make_workspace(tmp_path, {})  # no standards files present
        monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(on_pr_review, "_PROJECT_ROOT", str(ws))

        with pytest.raises(RuntimeError, match="standards"):
            on_pr_review._build_context(diff="", pr_number=1, repo="owner/repo")

    def test_missing_required_anti_patterns_catalog_fails_loud(
        self, tmp_path, monkeypatch
    ):
        """A missing catalog must stop review rather than silently degrading it."""
        from reviewer import on_pr_review

        ws = _make_workspace(
            tmp_path,
            {
                "DEVELOPMENT-DOCTRINE.md": _DEVELOPMENT_DOCTRINE_FIXTURE,
                "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": _ENG_ARCH_FIXTURE,
                "PRODUCT-STANDARDS.md": _PRODUCT_FIXTURE,
                "COPY-STANDARD.md": _COPY_FIXTURE,
            },
        )
        monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(on_pr_review, "_PROJECT_ROOT", str(ws))

        with pytest.raises(RuntimeError, match="anti-patterns|catalog|missing|empty"):
            on_pr_review._build_context(diff="", pr_number=1, repo="owner/repo")

    def test_missing_copy_standard_fails_loud(self, tmp_path, monkeypatch):
        """Copy is required: its absence stops review rather than skipping it."""
        from reviewer import on_pr_review

        ws = _make_workspace(
            tmp_path,
            {
                "DEVELOPMENT-DOCTRINE.md": _DEVELOPMENT_DOCTRINE_FIXTURE,
                "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": _ENG_ARCH_FIXTURE,
                "PRODUCT-STANDARDS.md": _PRODUCT_FIXTURE,
                "llm-anti-patterns.md": "# LLM anti-pattern catalog\n",
            },
        )
        monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(on_pr_review, "_PROJECT_ROOT", str(ws))

        with pytest.raises(RuntimeError, match="COPY-STANDARD"):
            on_pr_review._build_context(diff="", pr_number=1, repo="owner/repo")

    def test_empty_standard_file_fails_loud(self, tmp_path, monkeypatch):
        """An empty/whitespace standards file must error — not pass an empty contract."""
        from reviewer import on_pr_review

        ws = _make_workspace(
            tmp_path,
            {
                "DEVELOPMENT-DOCTRINE.md": _DEVELOPMENT_DOCTRINE_FIXTURE,
                "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": "   \n",  # empty
                "PRODUCT-STANDARDS.md": _PRODUCT_FIXTURE,
            },
        )
        monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(on_pr_review, "_PROJECT_ROOT", str(ws))

        with pytest.raises(RuntimeError, match="ENGINEERING|missing|empty"):
            on_pr_review._build_context(diff="", pr_number=1, repo="owner/repo")

    def test_unknown_required_standard_key_errors_clearly(self, tmp_path, monkeypatch):
        """A required key with no registry entry → clear error, not opaque KeyError."""
        from reviewer import on_pr_review

        ws = self._ws_with_standards(tmp_path)
        monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(ws))
        monkeypatch.setattr(on_pr_review, "_PROJECT_ROOT", str(ws))
        monkeypatch.setattr(
            on_pr_review, "_CODE_REVIEW_STANDARDS", ["eng-arch", "bogus-key"]
        )

        with pytest.raises(RuntimeError, match="bogus-key"):
            on_pr_review._build_context(diff="", pr_number=1, repo="owner/repo")
