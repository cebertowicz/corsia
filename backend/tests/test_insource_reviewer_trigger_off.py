"""PR review runs as an event-rule workflow: CORSIA keeps no code that starts or runs a reviewer."""

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REMOVED_MODULES = (
    "events.reviewer_gate",
    "events.reviewer_route",
    "events.connectors.github_reviewer_gate",
    "events.connectors.github_reviewer_gate_recovery",
    "events.connectors.github_reviewer_fork_hold",
    "events.connectors.github_reviewer_notifications",
    "reviewer.on_pr_review",
    "reviewer.reviewer_runner",
    "reviewer.reviewer_backends",
    "reviewer.reviewer_status",
    "prompts.reviewer",
    "automation.review_intent",
)

GATE_EVENTS = (
    ("webhook.github.check_suite_completed", {
        "action": "completed", "repository": {"full_name": "o/r"},
        "check_suite": {"head_sha": "abc123", "head_branch": "b", "conclusion": "success",
                        "pull_requests": [{"number": 7, "head": {"sha": "abc123", "ref": "b"},
                                           "base": {"ref": "dev"}}]}}),
    ("webhook.github.pr_ready_for_review", {
        "action": "ready_for_review", "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc123", "repo": {"full_name": "o/r"}},
                         "base": {"ref": "dev", "repo": {"full_name": "o/r"}}, "labels": []}}),
    ("webhook.github.pr_labeled", {
        "action": "labeled", "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc123", "repo": {"full_name": "fork/r"}},
                         "base": {"ref": "dev", "repo": {"full_name": "o/r"}}, "labels": []},
        "label": {"name": "review"}, "sender": {"login": "maintainer", "type": "User"}}),
)


@pytest.mark.parametrize("name", REMOVED_MODULES)
def test_the_reviewer_modules_are_gone(name: str) -> None:
    assert importlib.util.find_spec(name) is None


def test_ingress_only_routes_for_every_gate_event() -> None:
    from events import ingress

    routed = []

    async def fake_route(internal_event, payload, delivery_id=None):
        routed.append(internal_event)
        return None

    assert not hasattr(ingress, "review_leg") and not hasattr(ingress, "gate_trigger_leg")
    with patch("events.ingress.route_leg", fake_route):
        for event, payload in GATE_EVENTS:
            result = asyncio.run(ingress.handle_event(event, payload))
            assert "reviewer" not in result and "reviewer_gate" not in result
    assert routed == [event for event, _ in GATE_EVENTS]


def test_a_rule_worker_copies_no_head_and_reads_only_staging_and_its_empty_start_directory(tmp_path) -> None:
    from automation import worker_inputs, worker_runner

    for name in ("head_source", "write_head", "HEAD_REPO_FACT"):
        assert not hasattr(worker_inputs, name), name
    root, staging = tmp_path / "owner", tmp_path / "owner" / "staging" / "req"
    staging.mkdir(parents=True)
    body = tmp_path / "SKILL.md"
    body.write_text("---\nname: w\n---\nDo the work.\n")
    found = {"root": root, "staging": str(staging), "skill": SimpleNamespace(body_path=body),
             "schema": {"type": "object"}, "workflow": "w@1"}
    event = {"type": "request", "facts": {"head_repo": "app", "head_sha": "a" * 40}}

    read_paths, task = worker_runner._write(found, 7, {"event": event})

    start = root / "workers" / "7"
    assert read_paths == [str(staging), str(start)]
    assert str(staging) in task and "head_repo" in task  # only a fact the worker sees
    assert start.is_dir() and list(start.iterdir()) == []  # no head tree is written
    assert sorted(path.name for path in root.iterdir()) == ["staging", "workers"]
