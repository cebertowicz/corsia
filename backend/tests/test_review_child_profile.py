"""CI-only proof for the Claude Code confined review-child profile.

This intentionally launches the stock CLI, not a mocked command builder.  The
fixture controls the model responses and records the tool results the CLI sends
back, so each denied operation fails this test if it starts succeeding.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from cc_receipt_stock_fixtures import read_request, sse
from contract.matrix_environment import isolated_environment
from reviewer.reviewer_exact_source import (
    claude_config_state_scope,
    exact_review_source,
    render_write_profile,
)
from subagents.cc_env import clean_claude_env
from subagents.cc_invocation import claude_review_profile_args


CLAUDE_MD_MARKER = "REVIEW_CHILD_MUST_NOT_LOAD_THIS_CLAUDE_MD"
HOOK_MARKER = "review-child-hook-ran"
HEAD_CONTENT = "HEAD_TREE_CONTENT_8c3d\n"
OUTSIDE_CONTENT = "OUTSIDE_BRIEF_CONTENT_6f2a\n"
_FORBIDDEN_ENV_NAMES = ("GH_TOKEN", "CORSIA_AGENT_TOKEN", "CORSIA_DELEGATE_TOKEN")


def _can_apply_sandbox() -> bool:
    if sys.platform != "darwin":
        return False
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
    ).returncode == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True,
        capture_output=True,
    ).stdout.strip()


def _fixed_sha_tree(repo: Path, base: Path) -> tuple[Path, str]:
    """Materialize a head only after grounding it through the review object store."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review-child@example.test")
    _git(repo, "config", "user.name", "Review Child")
    (repo / "allowed.txt").write_text(HEAD_CONTENT)
    (repo / "CLAUDE.md").write_text(CLAUDE_MD_MARKER)
    (repo / ".claude").mkdir()
    hook_target = base / HOOK_MARKER
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command":
            f"touch {hook_target}"}]}],
    }}))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "review head")
    head_sha = _git(repo, "rev-parse", "HEAD")

    with exact_review_source(str(repo), head_sha, head_sha) as (_, neutral_dir, git_dir):
        assert Path(neutral_dir).parent != repo
        archive = subprocess.run(
            ["git", f"--git-dir={git_dir}", "archive", "--format=tar", head_sha],
            check=True, capture_output=True,
        ).stdout
    tree = base / "fixed-head"
    tree.mkdir()
    with tarfile.open(fileobj=BytesIO(archive)) as stream:
        stream.extractall(tree, filter="data")
    return tree, head_sha


def _tool_result(body: dict) -> list[dict]:
    messages = body.get("messages", [])
    if not messages or not isinstance(messages[-1], dict):
        return []
    content = messages[-1].get("content", [])
    return [item for item in content if isinstance(item, dict) and item.get("type") == "tool_result"]


def _as_text(result: dict) -> str:
    content = result.get("content", "")
    if isinstance(content, list):
        return "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def _denied(result: dict) -> bool:
    # Only the CLI's own error flag counts: matching words in the output would
    # pass a tool that succeeded and happened to print "permission".
    return bool(result.get("is_error"))


def _skip_or_fail(reason: str) -> None:
    # In CI this test is the confinement proof, so a missing prerequisite
    # fails instead of reporting a green skip that proves nothing.
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


class _ReviewPeer:
    def __init__(self, checks: list[tuple[str, dict]]) -> None:
        self.checks = checks
        self.bodies: list[dict] = []
        self.results: dict[str, dict] = {}
        self._next = 0

    async def respond(self, reader, writer) -> None:
        try:
            _, route, raw = await read_request(reader)
            body = json.loads(raw) if raw else {}
            if "/v1/messages/count_tokens" in route:
                data, content_type = b'{"input_tokens":1}', "application/json"
            elif "/v1/messages" in route:
                self.bodies.append(body)
                for result in _tool_result(body):
                    self._record_result(result)
                data = self._reply(body.get("model", "fixture"))
                content_type = "text/event-stream"
            else:
                data, content_type = b"{}", "application/json"
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _record_result(self, result: dict) -> None:
        """Keep one durable result per model-issued tool use.

        Claude Code can replay an already-completed tool result in a later
        model request (for example after adding its system reminder). The tool
        use ID, not the HTTP request, is the fixture's causal identity.
        """
        tool_use_id = result.get("tool_use_id")
        assert isinstance(tool_use_id, str) and tool_use_id
        self.results.setdefault(tool_use_id, result)

    def _reply(self, model: str) -> bytes:
        block: dict[str, object]
        delta: bytes
        if self._next == len(self.checks):
            block = {"type": "text", "text": "profile checks complete"}
            delta = sse("content_block_delta", index=0, delta={"type": "text_delta", "text": ""})
            stop = "end_turn"
        else:
            name, arguments = self.checks[self._next]
            self._next += 1
            block = {"type": "tool_use", "id": f"review-check-{self._next}", "name": name, "input": {}}
            delta = sse("content_block_delta", index=0, delta={
                "type": "input_json_delta", "partial_json": json.dumps(arguments),
            })
            stop = "tool_use"
        return (
            sse("message_start", message={"id": f"review-{self._next}", "type": "message", "role": "assistant",
                "content": [], "model": model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}})
            + sse("content_block_start", index=0, content_block=block)
            + delta
            + sse("content_block_stop", index=0)
            + sse("message_delta", delta={"stop_reason": stop, "stop_sequence": None}, usage={"output_tokens": 1})
            + sse("message_stop")
        )


def test_review_profile_args_leave_only_read_tools_available():
    args = claude_review_profile_args()

    assert args[:6] == [
        "--restricted", "--safe-mode", "--setting-sources", "",
        "--strict-mcp-config", "--permission-mode",
    ]
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--allowedTools") + 1:args.index("--disallowedTools")] == [
        "Read", "Grep", "Glob",
    ]
    assert args[args.index("--disallowedTools") + 1:] == [
        "Bash", "WebFetch", "WebSearch", "Write", "Edit", "Task",
    ]


def test_review_peer_deduplicates_replayed_result_by_tool_use_id():
    peer = _ReviewPeer([])
    result = {"type": "tool_result", "tool_use_id": "review-check-1", "content": "first"}

    peer._record_result(result)
    peer._record_result({**result, "content": "replayed"})

    assert peer.results == {"review-check-1": result}


@pytest.mark.integration
async def test_review_child_profile_confines_stock_claude_to_the_fixed_head():
    if not _can_apply_sandbox():
        _skip_or_fail("needs macOS sandbox-exec outside the agent shell; CI L3 provides it")
    binary = shutil.which("claude")
    if not binary:
        _skip_or_fail("requires the pinned stock Claude CLI installed by CI L3")

    base = Path(tempfile.mkdtemp(prefix="review-child-profile-")).resolve()
    server = None
    try:
        repo = base / "source-repo"
        repo.mkdir()
        head_tree, _ = _fixed_sha_tree(repo, base)
        run_dir = base / "neutral-run-directory-4f30a1"
        run_dir.mkdir()
        assert head_tree != run_dir and head_tree not in run_dir.parents
        outside = base / "outside-brief.txt"
        outside.write_text(OUTSIDE_CONTENT)
        write_target = head_tree / "written-by-review-child.txt"

        checks = [
            ("Read", {"file_path": str(head_tree / "allowed.txt")}),
            ("Read", {"file_path": str(outside)}),
            ("Write", {"file_path": str(write_target), "content": "must not write"}),
            ("Edit", {"file_path": str(head_tree / "allowed.txt"), "old_string": HEAD_CONTENT, "new_string": "changed"}),
            ("Bash", {"command": "env"}),
            ("mcp__corsia__read_file", {"path": str(head_tree / "allowed.txt")}),
            ("WebFetch", {"url": "https://example.invalid/review-child"}),
        ]
        peer = _ReviewPeer(checks)
        server = await asyncio.start_server(peer.respond, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        # Start from the backend's scrubbed Claude environment, then use a
        # fixture-owned allowlist so neither tokens nor the person's gh home can leak in.
        env = {key: value for key, value in clean_claude_env().items() if key not in _FORBIDDEN_ENV_NAMES}
        env.update(isolated_environment(base, base / "workspace", base / "state"))
        env.update({
            "ANTHROPIC_BASE_URL": f"http://localhost:{port}",
            "ANTHROPIC_API_KEY": "fixture-key-only",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        })
        assert not any(name in env for name in _FORBIDDEN_ENV_NAMES)
        assert Path(env["HOME"]).parent == base

        scope = replace(claude_config_state_scope(env, str(run_dir)), allow_global_file=True)
        profile = render_write_profile([], scope) + "(deny network*)" + (
            f'(allow network-outbound (remote ip "localhost:{port}"))'
        )
        command = [
            "/usr/bin/sandbox-exec", "-p", profile, binary, "-p", "Run each requested tool call.",
            "--output-format", "json", "--model", "claude-sonnet-4-6", "--add-dir", str(head_tree),
            *claude_review_profile_args(),
        ]
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(run_dir), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        assert peer.bodies, f"stock Claude sent no request: {stderr.decode()[-600:]}"
        assert process.returncode == 0, stderr.decode()[-600:]
        expected_ids = {f"review-check-{index}" for index in range(1, len(checks) + 1)}
        assert set(peer.results) == expected_ids, f"unexpected tool-result IDs: {set(peer.results)}"

        tool_results = [peer.results[f"review-check-{index}"] for index in range(1, len(checks) + 1)]
        assert HEAD_CONTENT in _as_text(tool_results[0])
        outside_result = tool_results[1]
        assert _denied(outside_result), _as_text(outside_result)
        assert OUTSIDE_CONTENT not in _as_text(outside_result)
        for name, result in zip(("Write", "Edit", "Bash", "mcp__corsia__read_file", "WebFetch"), tool_results[2:]):
            assert _denied(result), f"{name} unexpectedly succeeded: {_as_text(result)}"
        assert not write_target.exists()
        assert (head_tree / "allowed.txt").read_text() == HEAD_CONTENT
        assert not (base / HOOK_MARKER).exists()
        assert CLAUDE_MD_MARKER not in json.dumps(peer.bodies)
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        shutil.rmtree(base, ignore_errors=True)
