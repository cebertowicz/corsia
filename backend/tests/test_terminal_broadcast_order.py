"""Every terminal producer commits durable Turn state before it broadcasts.

The canonical settlement helpers have their own transaction tests. This matrix
pins the other half of the contract at every producer: publication must remain
textually downstream of the helper that returns only after durable settlement.
A newly added terminal path must join this matrix rather than relying on a
fresh-connection race to expose an ordering regression.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

# id, source, function, durable-settlement call, terminal-publication call
_CASES = (
    ("native-complete", "core/native_turn_runtime.py", "_finalize_native_turn",
     "_claim_native_terminal", "_publish_terminal_effects"),
    ("native-cancel", "core/native_turn_runtime.py", "_finalize_native_turn",
     "_claim_native_terminal", "_publish_terminal_effects"),
    ("native-error", "core/native_turn_runtime.py", "_finalize_native_turn",
     "_claim_native_terminal", "_publish_terminal_effects"),
    ("codex-result", "subagents/codex_runtime_events.py", "_on_result",
     "commit_turn_terminal", "_publish_claimed_terminal"),
    ("codex-failure", "subagents/codex_runtime_events.py", "_on_turn_failed",
     "commit_turn_terminal", "_publish_claimed_terminal"),
    ("claude-result", "subagents/cc_event_result.py", "on_result",
     "claim_terminal", "_finish_result_postprocessing"),
    ("claude-process-exit", "subagents/cc_process_exit.py", "finish_process_exit",
     "_settle_process_exit", "_publish_process_exit"),
    ("claude-preempt", "subagents/cc_preempt.py", "commit_interrupted_terminal",
     "claim_terminal", "bcast"),
    ("claude-unverified-delivery", "subagents/cc_accept.py",
     "finish_unverified_input_delivery", "claim_terminal", "bcast"),
    ("websocket-stop", "ws/stop_control.py", "_stop_exact_turn",
     "_commit_stop_terminal", "state.broadcast_event"),
)


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(), str(path))
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"expected one {name} in {path}"
    return matches[0]


def _call_lines(function: ast.AST, marker: str) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and marker in ast.unparse(node)
    )


@pytest.mark.parametrize(
    ("_case", "relative", "function_name", "commit_marker", "publish_marker"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_terminal_publication_follows_durable_settlement(
    _case: str,
    relative: str,
    function_name: str,
    commit_marker: str,
    publish_marker: str,
) -> None:
    function = _function(_BACKEND / relative, function_name)
    commits = _call_lines(function, commit_marker)
    publications = _call_lines(function, publish_marker)

    assert commits, f"{_case}: durable settlement call disappeared"
    assert publications, f"{_case}: terminal publication call disappeared"
    assert max(commits) < min(publications), (
        f"{_case}: terminal publication can run before durable settlement"
    )
