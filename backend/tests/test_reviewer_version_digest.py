"""Tests for the reviewer-version digest on owner verdict events."""

from __future__ import annotations


def test_reviewer_version_digest_is_deterministic():
    from reviewer.reviewer_context import reviewer_version_digest

    standards = {"product": "product standard", "eng-arch": "engineering standard"}

    assert reviewer_version_digest("prompt", standards, "model-a") == reviewer_version_digest(
        "prompt", standards, "model-a"
    )


def test_reviewer_version_ignores_review_specific_inputs():
    from prompts.reviewer import build_reviewer_prompt, reviewer_prompt_version_text
    from reviewer.reviewer_context import reviewer_version_digest

    first_prompt = build_reviewer_prompt(
        "first diff",
        "first context",
        model_label="model-a",
        change_source="first source",
        review_history="first history",
    )
    second_prompt = build_reviewer_prompt(
        "second diff",
        "second context",
        model_label="model-a",
        change_source="second source",
        review_history="second history",
    )
    standards = {"product": "product standard"}

    assert first_prompt != second_prompt
    assert reviewer_version_digest(
        reviewer_prompt_version_text(), standards, "model-a"
    ) == reviewer_version_digest(reviewer_prompt_version_text(), standards, "model-a")


def test_reviewer_version_digest_changes_for_template_edit(monkeypatch):
    from prompts import reviewer as reviewer_prompt
    from reviewer.reviewer_context import reviewer_version_digest

    standards = {"product": "product standard"}
    original = reviewer_version_digest(
        reviewer_prompt.reviewer_prompt_version_text(), standards, "model-a"
    )
    monkeypatch.setattr(reviewer_prompt, "_TEMPLATE", f"{reviewer_prompt._TEMPLATE}\nnew rule")

    assert reviewer_version_digest(
        reviewer_prompt.reviewer_prompt_version_text(), standards, "model-a"
    ) != original


def test_reviewer_version_digest_changes_for_standard_content():
    from reviewer.reviewer_context import reviewer_version_digest

    original = {"product": "before", "eng-arch": "unchanged"}
    changed = {**original, "product": "after"}

    assert reviewer_version_digest("prompt", original, "model-a") != reviewer_version_digest(
        "prompt", changed, "model-a"
    )


def test_reviewer_version_digest_returns_after_standard_content_restore():
    from reviewer.reviewer_context import reviewer_version_digest

    original = {"product": "before"}
    changed = {"product": "after"}
    original_digest = reviewer_version_digest("prompt", original, "model-a")

    assert reviewer_version_digest("prompt", changed, "model-a") != original_digest
    assert reviewer_version_digest("prompt", original, "model-a") == original_digest


def test_reviewer_version_digest_changes_for_model():
    from reviewer.reviewer_context import reviewer_version_digest

    standards = {"product": "standard"}

    assert reviewer_version_digest("prompt", standards, "model-a") != reviewer_version_digest(
        "prompt", standards, "model-b"
    )


def test_reviewer_verdict_event_includes_version_and_standards(monkeypatch):
    from events.connectors import github_reviewer_notifications

    captured: dict[str, object] = {}

    def deliver_owner_event(*args, **kwargs):
        captured["payload"] = args[4]
        return "delivered"

    monkeypatch.setattr(
        github_reviewer_notifications, "deliver_owner_event", deliver_owner_event
    )

    result = github_reviewer_notifications.notify_reviewer_verdict(
        "owner/repo",
        7,
        "abc123",
        {
            "verdict": "clean",
            "reviewer_version": "0123456789ab",
            "reviewer_standards": ["eng-arch", "product"],
        },
    )

    assert result == "delivered"
    assert captured["payload"] == {
        "repo": "owner/repo",
        "pr_number": 7,
        "head_sha": "abc123",
        "verdict": "clean",
        "required_action": "none",
        "summary": None,
        "block_count": 0,
        "flag_count": 0,
        "nit_count": 0,
        "finding_count": 0,
        "model": None,
        "reviewer_version": "0123456789ab",
        "reviewer_standards": ["eng-arch", "product"],
    }
