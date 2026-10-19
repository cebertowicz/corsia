"""Tests for reviewer-version attribution on verdict events."""

from __future__ import annotations

from pathlib import Path


_STANDARD_FILES = {
    "DEVELOPMENT-DOCTRINE.md": "# Doctrine\n\n## Reviewer Contract\n- Block: doctrine\n",
    "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md": "# Engineering\n\n## Reviewer Contract\n- Block: engineering\n",
    "PRODUCT-STANDARDS.md": (
        "# Product\n\nOutside before.\n\n## Reviewer Contract\n"
        "- Block: product before\n\n## Appendix\nOutside after.\n"
    ),
    "COPY-STANDARD.md": "# Copy\n\n## Reviewer Contract\n- Block: copy\n",
    "llm-anti-patterns.md": "# Anti-patterns\n- Never hide a failure.\n",
}


def _write_standards(workspace: Path) -> Path:
    """Write the complete always-on reviewer standards snapshot."""
    standards = workspace / "projects" / "corsia" / "shared" / "standards"
    standards.mkdir(parents=True)
    for name, content in _STANDARD_FILES.items():
        (standards / name).write_text(content, encoding="utf-8")
    return standards


def _build_context(monkeypatch, workspace: Path):
    """Build one context without adding a PRD contract to the fixture."""
    from reviewer import on_pr_review

    monkeypatch.setattr(on_pr_review, "_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(on_pr_review, "_resolve_prd_contracts", lambda *_args: [])
    return on_pr_review._build_context("diff", 1, "owner/repo")


def test_reviewer_version_digest_is_deterministic():
    from reviewer.reviewer_context import reviewer_version_digest

    assert reviewer_version_digest("prompt", "standards") == reviewer_version_digest(
        "prompt", "standards"
    )


def test_reviewer_version_hashes_only_injected_standard_text(monkeypatch, tmp_path):
    standards = _write_standards(tmp_path)
    original = _build_context(monkeypatch, tmp_path)
    product = standards / "PRODUCT-STANDARDS.md"

    product.write_text(
        _STANDARD_FILES["PRODUCT-STANDARDS.md"].replace("Outside after.", "Changed prose."),
        encoding="utf-8",
    )
    outside_edit = _build_context(monkeypatch, tmp_path)
    product.write_text(
        _STANDARD_FILES["PRODUCT-STANDARDS.md"].replace("product before", "product after"),
        encoding="utf-8",
    )
    inside_edit = _build_context(monkeypatch, tmp_path)
    product.write_text(_STANDARD_FILES["PRODUCT-STANDARDS.md"], encoding="utf-8")
    restored = _build_context(monkeypatch, tmp_path)

    assert outside_edit.reviewer_version == original.reviewer_version
    assert inside_edit.reviewer_version != original.reviewer_version
    assert restored.reviewer_version == original.reviewer_version


def test_reviewer_version_template_uses_the_prompt_renderer(monkeypatch):
    from prompts import reviewer as reviewer_prompt
    from reviewer.reviewer_context import reviewer_version_digest

    template = reviewer_prompt.reviewer_prompt_version_text()
    assert template == reviewer_prompt.build_reviewer_prompt(
        "",
        "{context_text}",
        model_label="{model_label}",
        change_source="{change_source}",
        review_history="{review_history}",
    )
    original = reviewer_version_digest(template, "standards")
    monkeypatch.setattr(
        reviewer_prompt,
        "VALID_CATEGORIES",
        (*reviewer_prompt.VALID_CATEGORIES, "test/category"),
    )

    assert reviewer_version_digest(
        reviewer_prompt.reviewer_prompt_version_text(), "standards"
    ) != original


def test_cloud_and_local_paths_use_one_precomputed_version(monkeypatch):
    from reviewer import on_pr_review

    context = on_pr_review._ReviewContext(
        text="context", reviewer_version="0123456789ab", reviewer_standards=["product"],
    )
    monkeypatch.setattr(on_pr_review, "_build_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(on_pr_review, "is_27b_loaded", lambda: True)
    monkeypatch.setattr(on_pr_review, "set_pending", lambda *_args: None)
    monkeypatch.setattr(on_pr_review, "_review_diff", lambda *_args: "diff")
    monkeypatch.setattr(on_pr_review, "build_review_history", lambda _comments: "history")
    monkeypatch.setattr(on_pr_review, "invoke_local_model", lambda _prompt: "[]")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        on_pr_review,
        "_process_findings",
        lambda *_args, **kwargs: captured.append(kwargs) or {"verdict": "clean"},
    )

    on_pr_review._run_local(1, "owner/repo", "head", {}, ([], []))

    assert captured == [{
        "fallback_file": "",
        "reviewer_version": "0123456789ab",
        "reviewer_standards": ["product"],
    }]


def test_reviewer_verdict_event_includes_version_and_standards(monkeypatch):
    from events.connectors import github_reviewer_notifications

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        github_reviewer_notifications,
        "deliver_owner_event",
        lambda *args, **_kwargs: captured.update(payload=args[4]) or "delivered",
    )

    result = github_reviewer_notifications.notify_reviewer_verdict(
        "owner/repo", 7, "abc123", {
            "verdict": "clean",
            "reviewer_version": "0123456789ab",
            "reviewer_standards": ["eng-arch", "product"],
        },
    )

    assert result == "delivered"
    assert captured["payload"]["reviewer_version"] == "0123456789ab"
    assert captured["payload"]["reviewer_standards"] == ["eng-arch", "product"]


def test_ownerless_reviewer_verdict_event_includes_version_and_standards(monkeypatch):
    from events.connectors import github_reviewer_notifications

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        github_reviewer_notifications, "deliver_owner_event", lambda *_args: "no_owner"
    )
    monkeypatch.setattr(
        github_reviewer_notifications,
        "emit_product_event",
        lambda *_args, **kwargs: captured.update(kwargs["properties"]),
    )

    github_reviewer_notifications.notify_reviewer_verdict(
        "owner/repo", 7, "abc123def", {
            "verdict": "clean",
            "reviewer_version": "0123456789ab",
            "reviewer_standards": ["eng-arch", "product"],
        },
    )

    assert captured["reviewer_version"] == "0123456789ab"
    assert captured["reviewer_standards"] == ["eng-arch", "product"]


def test_reviewer_events_include_version_and_standards(monkeypatch):
    from reviewer import on_pr_review

    events: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(on_pr_review, "parse_findings", lambda _raw: [{
        "category": "standards/product", "severity": "block", "file": "x.py",
    }])
    monkeypatch.setattr(on_pr_review, "prepare_findings_for_publication", lambda findings, **_kwargs: findings)
    monkeypatch.setattr(on_pr_review, "format_status_summary", lambda *_args: ("none", "clean", 0, 0, 0))
    monkeypatch.setattr(on_pr_review, "fetch_existing_review_comments", lambda *_args: [])
    monkeypatch.setattr(on_pr_review, "fetch_existing_issue_comments", lambda *_args: [])
    monkeypatch.setattr(on_pr_review, "classify_review_findings", lambda *_args, **_kwargs: ([], [], 0))
    monkeypatch.setattr(on_pr_review, "publish_review", lambda **_kwargs: None)
    monkeypatch.setattr(on_pr_review, "set_success", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        on_pr_review,
        "emit_product_event",
        lambda domain, event, *, properties: events.append((domain, event, properties)),
    )

    on_pr_review._process_findings(
        "[]", 7, "owner/repo", "abc123", "model-a",
        reviewer_version="0123456789ab",
        reviewer_standards=["eng-arch", "product"],
    )

    attributes = [properties for _, event, properties in events if event in {"finding.created", "completed"}]
    assert all(event["reviewer_version"] == "0123456789ab" for event in attributes)
    assert all(event["reviewer_standards"] == ["eng-arch", "product"] for event in attributes)
