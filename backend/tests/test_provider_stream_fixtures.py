"""The fixture validator must reject anything that would certify a bad refactor.

The replay suite's whole worth is that its fixtures are real. So the cases that
matter here are the refusals: a hand-authored file, an unscrubbed capture, and
a stream that parses but reaches no branch of the pump.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.provider_stream_fixtures import (
    CC_DISPATCH_KINDS,
    CODEX_DISPATCH_KINDS,
    FixtureError,
    load_fixture,
    validate_fixture,
)

_BACKEND = Path(__file__).parent.parent

_GOOD_META = {
    "capture_method": "stream_capture_tap",
    "captured_at": "2026-08-15T21:00:00Z",
    "provider_version": "claude-code 2.1.218",
    "scenario": "tool_heavy",
    "scrubbed": True,
    "thread_id": "thread-scratch-1",
}

# A minimal real-shaped CC stream: init, assistant text, terminal result.
_GOOD_FRAMES = [
    {"type": "system", "subtype": "init", "thread_id": "cc-1", "model": "m",
     "tools": [], "mcp_servers": [], "cwd": "."},
    {"type": "assistant",
     "message": {"content": [{"type": "text", "text": "hello"}]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "hello", "stop_reason": "end_turn"},
]


def _write(tmp_path, name="cc-tool_heavy", frames=None, meta=None):
    root = tmp_path / "provider_streams"
    root.mkdir(exist_ok=True)
    path = root / f"{name}.jsonl"
    path.write_text(
        "".join(json.dumps(f) + "\n" for f in (
            _GOOD_FRAMES if frames is None else frames
        ))
    )
    payload = dict(_GOOD_META) if meta is None else meta
    (root / f"{name}.meta.json").write_text(json.dumps(payload))
    return path


def _dispatch_keys_in_source(relative: str, variable: str) -> set[str]:
    """Read the pump's real dispatch table out of its source.

    Extracted rather than imported because the table is a local inside the
    pump factory. If this extraction ever fails it fails loudly, which is the
    point: a validator that silently stops tracking the pump is the same class
    of defect it exists to catch.
    """
    tree = ast.parse((_BACKEND / relative).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if variable in targets:
            return {
                key.value for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError(f"no dict assigned to {variable} in {relative}")


def test_cc_dispatch_kinds_match_the_live_pump():
    """A pump that gains a branch must not leave the validator behind."""
    from subagents.cc_runtime_events import _dispatch

    assert set(_dispatch()) == set(CC_DISPATCH_KINDS)


def test_codex_dispatch_kinds_match_the_live_pump():
    assert _dispatch_keys_in_source(
        "subagents/codex_runtime_events.py", "_dispatch",
    ) == set(CODEX_DISPATCH_KINDS)


def test_a_real_shaped_capture_validates(tmp_path):
    assert validate_fixture(load_fixture(_write(tmp_path))) == []


def test_a_hand_authored_fixture_is_rejected(tmp_path):
    """The defect this whole format exists to prevent."""
    meta = dict(_GOOD_META, capture_method="hand_authored")
    problems = validate_fixture(load_fixture(_write(tmp_path, meta=meta)))
    assert any("not a recorded capture" in p for p in problems)


def test_a_capture_without_provenance_is_rejected(tmp_path):
    path = _write(tmp_path)
    path.with_suffix(".meta.json").unlink()
    with pytest.raises(FixtureError, match="without provenance"):
        load_fixture(path)


@pytest.mark.parametrize(
    "field", ["captured_at", "provider_version", "scenario", "scrubbed"],
)
def test_incomplete_provenance_is_rejected(tmp_path, field):
    meta = {k: v for k, v in _GOOD_META.items() if k != field}
    problems = validate_fixture(load_fixture(_write(tmp_path, meta=meta)))
    assert any(field in p for p in problems)


def test_an_unscrubbed_capture_is_rejected(tmp_path):
    meta = dict(_GOOD_META, scrubbed=False)
    problems = validate_fixture(load_fixture(_write(tmp_path, meta=meta)))
    assert any("scrubbed" in p for p in problems)


def test_host_paths_and_credentials_are_caught(tmp_path):
    leaky = [
        dict(_GOOD_FRAMES[0], cwd="/Users/alice/corsia/projects/corsia"),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "key sk-abcdefghijklmnopqrstuvwxyz"},
        ]}},
        _GOOD_FRAMES[2],
    ]
    problems = validate_fixture(load_fixture(_write(tmp_path, frames=leaky)))
    assert any("macOS home path" in p for p in problems)
    assert any("API key" in p for p in problems)


def test_a_foreign_thread_id_is_caught(tmp_path):
    frames = [
        _GOOD_FRAMES[0],
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "see thread-steves-real-work for context"},
        ]}},
        _GOOD_FRAMES[2],
    ]
    problems = validate_fixture(load_fixture(_write(tmp_path, frames=frames)))
    assert any("foreign thread id: thread-steves-real-work" in p for p in problems)


def test_a_stream_reaching_no_pump_branch_is_rejected(tmp_path):
    """Parses, normalizes, and exercises nothing — coverage that isn't."""
    frames = [{"type": "user", "message": {"content": "just a user echo"}}]
    problems = validate_fixture(load_fixture(_write(tmp_path, frames=frames)))
    assert any("exercises nothing" in p for p in problems)


def test_an_empty_capture_is_rejected(tmp_path):
    with pytest.raises(FixtureError, match="proves nothing"):
        load_fixture(_write(tmp_path, frames=[]))


def test_a_truncated_line_fails_loudly(tmp_path):
    path = _write(tmp_path)
    path.write_text(path.read_text() + '{"type": "result", "trunc\n')
    with pytest.raises(FixtureError, match="not JSON"):
        load_fixture(path)


def test_an_unknown_harness_prefix_is_rejected(tmp_path):
    with pytest.raises(FixtureError, match="must start with"):
        load_fixture(_write(tmp_path, name="gemini-tool_heavy"))
