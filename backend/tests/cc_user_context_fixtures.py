"""Sentinel instruction files and a loopback peer for the stock-CLI proof (#1874).

A managed Claude Code session must carry only what CORSIA delivered. These
fixtures plant every user-level instruction and memory source the CLI can
discover, then capture what the process actually sends to the model, so the
test asserts on the request body rather than on our own launch arguments.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from cc_receipt_stock_fixtures import read_request, sse

SENTINELS = {
    "user_claude_md": "SENTINEL_USER_CLAUDE_MD_7f1",
    "user_import": "SENTINEL_USER_IMPORT_7f2",
    "user_rule": "SENTINEL_USER_RULE_7f3",
    "memory_index": "SENTINEL_MEMORY_INDEX_7f4",
    "ancestor_claude_md": "SENTINEL_ANCESTOR_CLAUDE_MD_7f5",
    "cwd_claude_local": "SENTINEL_CWD_CLAUDE_LOCAL_7f6",
    "cwd_claude_md": "SENTINEL_CWD_CLAUDE_MD_7f7",
    "cwd_dot_claude_md": "SENTINEL_CWD_DOT_CLAUDE_MD_7f8",
}
_MEMORY_PROMPT_MARKERS = ("auto memory", "persistent, file-based memory", "memory.md")


def _turn(model: str, block: dict, delta: dict, stop_reason: str) -> bytes:
    """Render one streamed assistant turn with a single content block."""
    return (
        sse("message_start", message={
            "id": "m", "type": "message", "role": "assistant", "content": [],
            "model": model, "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
        + sse("content_block_start", index=0, content_block=block)
        + sse("content_block_delta", index=0, delta=delta)
        + sse("content_block_stop", index=0)
        + sse("message_delta", delta={"stop_reason": stop_reason, "stop_sequence": None},
               usage={"output_tokens": 1})
        + sse("message_stop")
    )


def _completed_turn(model: str) -> bytes:
    """Render the smallest streamed assistant turn the CLI will accept."""
    return _turn(model, {"type": "text", "text": ""},
                 {"type": "text_delta", "text": "ok"}, "end_turn")


def _tool_use_turn(model: str, name: str, tool_input: dict) -> bytes:
    """Render an assistant turn that calls one tool."""
    block = {"type": "tool_use", "id": "toolu_peer_1", "name": name, "input": {}}
    delta = {"type": "input_json_delta", "partial_json": json.dumps(tool_input)}
    return _turn(model, block, delta, "tool_use")


class InferencePeer:
    """Answer the CLI with a synthetic turn and keep every request body.

    Given ``tool_use`` (a tool name and its input), the peer first calls that
    tool on the request that offers it, then answers once its result is back.
    """

    def __init__(self, tool_use: tuple[str, dict] | None = None) -> None:
        self.bodies: list[str] = []
        self._tool_use = tool_use

    def _answer(self, body: dict) -> bytes:
        model = body.get("model", "x")
        offered = {tool.get("name") for tool in body.get("tools") or [] if isinstance(tool, dict)}
        answered = '"tool_result"' in json.dumps(body.get("messages"))
        if self._tool_use and self._tool_use[0] in offered and not answered:
            return _tool_use_turn(model, *self._tool_use)
        return _completed_turn(model)

    async def respond(self, reader, writer) -> None:
        try:
            _, route, raw = await read_request(reader)
            if "/v1/messages" in route and "count_tokens" not in route:
                self.bodies.append(raw.decode())
                data = self._answer(json.loads(raw))
                content_type = "text/event-stream"
            else:
                data, content_type = b'{"input_tokens":1}', "application/json"
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
            )
            await writer.drain()
        finally:
            writer.close()

    def leaked(self) -> list[str]:
        """Name every sentinel that reached the model."""
        joined = "\n".join(self.bodies)
        return sorted(name for name, value in SENTINELS.items() if value in joined)

    def memory_prompt_present(self) -> bool:
        """Report whether the CLI added its own memory instructions."""
        joined = "\n".join(self.bodies).lower()
        return any(marker in joined for marker in _MEMORY_PROMPT_MARKERS)


def seed_user_context(base: Path, claude_root: Path) -> Path:
    """Plant every discoverable instruction source; return the working directory."""
    claude_root.mkdir(parents=True, exist_ok=True)
    (claude_root / "extra.md").write_text(SENTINELS["user_import"] + "\n")
    (claude_root / "CLAUDE.md").write_text(
        f"{SENTINELS['user_claude_md']}\n@{claude_root / 'extra.md'}\n")
    (claude_root / "rules").mkdir(exist_ok=True)
    (claude_root / "rules" / "x.md").write_text(SENTINELS["user_rule"] + "\n")
    ancestor = base / "ancestor"
    cwd = ancestor / "work"
    (cwd / ".claude").mkdir(parents=True, exist_ok=True)
    (ancestor / "CLAUDE.md").write_text(SENTINELS["ancestor_claude_md"] + "\n")
    (cwd / "CLAUDE.md").write_text(SENTINELS["cwd_claude_md"] + "\n")
    (cwd / ".claude" / "CLAUDE.md").write_text(SENTINELS["cwd_dot_claude_md"] + "\n")
    (cwd / "CLAUDE.local.md").write_text(SENTINELS["cwd_claude_local"] + "\n")
    # Auto memory keys its directory on the git root, so give cwd its own repo.
    subprocess.run(["git", "init", "-q", str(cwd)], check=True)
    _seed_memory_index(claude_root, cwd)
    return cwd


def _seed_memory_index(claude_root: Path, cwd: Path) -> None:
    """Write the memory index under every slug the CLI may derive from cwd."""
    resolved = str(cwd.resolve())
    dashed = resolved.replace("/", "-")
    for slug in {dashed, dashed.replace(".", "-"), dashed.replace("_", "-")}:
        memory = claude_root / "projects" / slug / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "MEMORY.md").write_text(f"- [{SENTINELS['memory_index']}](x.md) — hook\n")


async def serve(peer: InferencePeer) -> tuple[asyncio.AbstractServer, int]:
    """Start the loopback peer on an ephemeral port."""
    server = await asyncio.start_server(peer.respond, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]
