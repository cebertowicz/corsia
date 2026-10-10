
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import delegate_cli  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def _fake_request(method, base, path, token, body=None, query=None):
        calls.append({"method": method, "path": path, "body": body, "query": query})
        return {"ok": True, "thread_id": "target", "driver": "test"}

    monkeypatch.setattr(delegate_cli, "_resolve_token", lambda x: "tok")
    monkeypatch.setattr(delegate_cli, "_request", _fake_request)
    monkeypatch.setenv("CORSIA_THREAD_ID", "sender-self")
    return calls


def test_send_defaults_from_thread_id_from_corsia_thread_id(captured):
    delegate_cli.main(["send", "target-thread", "hello", "worker"])

    call = captured[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/delegation/threads/target-thread/send"
    assert call["body"] == {
        "message": "hello worker",
        "from_thread_id": "sender-self",
    }


def test_send_explicit_from_thread_id_overrides_env(captured):
    delegate_cli.main([
        "send",
        "target-thread",
        "hello",
        "--from-thread-id",
        "sender-explicit",
    ])

    assert captured[-1]["body"] == {
        "message": "hello",
        "from_thread_id": "sender-explicit",
    }


def test_send_passes_target_scope_id(captured):
    delegate_cli.main([
        "send",
        "target-thread",
        "--target-scope-id",
        "proj-b",
        "hello",
    ])

    assert captured[-1]["body"] == {
        "message": "hello",
        "from_thread_id": "sender-self",
        "target_scope_id": "proj-b",
    }


def test_create_subagent_uses_corsia_thread_id_as_parent(captured):
    delegate_cli.main([
        "create-thread",
        "--title",
        "Worker",
        "--model",
        "claude-code/sonnet",
        "--thread-kind",
        "subagent",
    ])

    call = captured[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/delegation/threads"
    assert call["body"] == {
        "title": "Worker",
        "model": "claude-code/sonnet",
        "thread_kind": "subagent",
        "from_thread_id": "sender-self",
    }


def test_create_spawned_by_overrides_create_from_thread_id(captured):
    delegate_cli.main([
        "create-thread",
        "--title",
        "Worker",
        "--model",
        "claude-code/sonnet",
        "--thread-kind",
        "subagent",
        "--spawned-by",
        "parent-explicit",
        "--from-thread-id",
        "sender-explicit",
    ])

    assert captured[-1]["body"] == {
        "title": "Worker",
        "model": "claude-code/sonnet",
        "thread_kind": "subagent",
        "spawned_by": "parent-explicit",
    }


def test_task_creates_hidden_child_and_sends_first_turn(captured, monkeypatch):
    def _fake_request(method, base, path, token, body=None, query=None):
        captured.append({"method": method, "path": path, "body": body, "query": query})
        if path == "/api/delegation/threads":
            return {"ok": True, "thread_id": "child-1", "model": body["model"]}
        return {"ok": True, "thread_id": "child-1", "driver": "test"}

    # Override the fixture's generic response so task can read the created id.
    monkeypatch.setattr(delegate_cli, "_request", _fake_request)

    delegate_cli.main([
        "task",
        "do",
        "this",
        "--title",
        "Worker",
        "--model",
        "claude-code/sonnet",
    ])

    assert captured[-2] == {
        "method": "POST",
        "path": "/api/delegation/threads",
        "body": {
            "title": "Worker",
            "thread_kind": "subagent",
            "from_thread_id": "sender-self",
            "model": "claude-code/sonnet",
        },
        "query": None,
    }
    assert captured[-1] == {
        "method": "POST",
        "path": "/api/delegation/threads/child-1/send",
        "body": {
            "message": "do this",
            "from_thread_id": "sender-self",
        },
        "query": None,
    }


def test_send_without_self_id_fails_loud_and_does_not_call_api(monkeypatch, capsys):
    # A delegated /send must be attributed (bug #362). With no CORSIA_THREAD_ID
    # and no --from-thread-id, the CLI must fail loud BEFORE the API call rather
    # than send an un-attributed user-masquerading message.
    calls: list[dict] = []

    def _fake_request(method, base, path, token, body=None, query=None):
        calls.append({"method": method, "path": path, "body": body, "query": query})
        return {"ok": True}

    monkeypatch.setattr(delegate_cli, "_resolve_token", lambda x: "tok")
    monkeypatch.setattr(delegate_cli, "_request", _fake_request)
    monkeypatch.delenv("CORSIA_THREAD_ID", raising=False)

    rc = delegate_cli.main(["send", "target-thread", "hello"])

    assert rc == 2
    assert calls == []  # never reached the API
    assert "must be attributed" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["create-thread", "--title", "W"],
    ["create-thread", "--model", "  "],
    ["task", "do", "this"],
    ["task", "do", "this", "--model", ""],
])
def test_create_and_task_reject_missing_or_blank_model_before_http(captured, argv):
    with pytest.raises(SystemExit) as exc:
        delegate_cli.main(argv)

    assert exc.value.code == 2
    assert captured == []
