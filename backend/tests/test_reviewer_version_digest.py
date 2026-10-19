"""Tests for the reviewer-version digest on owner verdict events."""

from __future__ import annotations


def test_reviewer_version_digest_is_deterministic():
    from reviewer.reviewer_context import reviewer_version_digest

    standards = {"product": "product standard", "eng-arch": "engineering standard"}

    assert reviewer_version_digest("prompt", standards, "model-a") == reviewer_version_digest(
        "prompt", standards, "model-a"
    )


def test_cloud_reviewer_version_ignores_review_specific_inputs(monkeypatch):
    from contextlib import contextmanager

    from reviewer import reviewer_runner

    @contextmanager
    def fake_exact_review_source(*_args):
        yield "reviewed source", "/tmp/review", "/tmp/source.git"

    monkeypatch.setattr(reviewer_runner, "exact_review_source", fake_exact_review_source)
    monkeypatch.setattr(reviewer_runner, "_run_cc_with_retries", lambda *_args, **_kwargs: "[]")
    versions: list[str | None] = []
    standards = {"product": "product standard"}

    reviewer_runner._attempt(
        "first diff", "first context", 1, "owner/repo", "first-head", "/tmp/work",
        "model-a", "model-a", "first-base", "first history",
        reviewer_standards=standards,
        reviewer_version_sink=versions.append,
    )
    reviewer_runner._attempt(
        "second diff", "second context", 2, "owner/repo", "second-head", "/tmp/work",
        "model-a", "model-a", "second-base", "second history",
        reviewer_standards=standards,
        reviewer_version_sink=versions.append,
    )

    assert versions[0] is not None
    assert versions[0] == versions[1]


def test_local_reviewer_version_ignores_review_specific_inputs(monkeypatch):
    from reviewer import on_pr_review

    monkeypatch.setattr(on_pr_review, "is_27b_loaded", lambda: True)
    monkeypatch.setattr(on_pr_review, "set_pending", lambda *_args: None)
    monkeypatch.setattr(on_pr_review, "_review_diff", lambda _payload, head: f"diff {head}")
    monkeypatch.setattr(on_pr_review, "_build_context", lambda diff, *_args, **_kwargs: f"context {diff}")
    monkeypatch.setattr(
        on_pr_review, "reviewer_standard_contents_for_keys", lambda *_args: {"product": "standard"}
    )
    monkeypatch.setattr(on_pr_review, "build_review_history", lambda _comments: "history")
    monkeypatch.setattr(on_pr_review, "invoke_local_model", lambda _prompt: "[]")
    versions: list[str | None] = []

    def capture_version(*_args, **kwargs):
        versions.append(kwargs["reviewer_version"])
        return {"verdict": "clean"}

    monkeypatch.setattr(on_pr_review, "_process_findings", capture_version)

    on_pr_review._run_local(1, "owner/repo", "first-head", {}, ([], []))
    on_pr_review._run_local(2, "owner/repo", "second-head", {}, ([], []))

    assert versions[0] is not None
    assert versions[0] == versions[1]


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
