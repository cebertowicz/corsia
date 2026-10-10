
from __future__ import annotations

import json
import os
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _write_history(root, entries):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "history.jsonl", "w") as f:
        for i, (sid, display) in enumerate(entries):
            f.write(json.dumps({"display": display, "pastedContents": {},
                                "timestamp": 1700000000000 + i,
                                "project": "/Users/x", "sessionId": sid}) + "\n")


@pytest.fixture
def claude_dir(tmp_path):
    root = tmp_path / "claude-home"
    proj_a = root / "projects" / "-Users-x-proj-a"
    proj_b = root / "projects" / "-Users-x-proj-b"

    _write_jsonl(proj_a / "aaaa-1111.jsonl", [
        {"type": "user", "cwd": "/Users/x/proj-a", "sessionId": "aaaa-1111",
         "timestamp": "2026-06-09T10:00:00Z",
         "message": {"role": "user", "content": "fix the login bug"}},
        {"type": "assistant", "cwd": "/Users/x/proj-a", "sessionId": "aaaa-1111",
         "timestamp": "2026-06-09T10:00:05Z",
         "message": {"content": [{"type": "text", "text": "On it."}]}},
        {"type": "last-prompt", "sessionId": "aaaa-1111", "lastPrompt": "now add tests"},
    ])
    _write_jsonl(proj_a / "bbbb-2222.jsonl", [
        {"type": "queue-operation", "sessionId": "bbbb-2222", "content": "queued thing"},
        {"type": "user", "cwd": "/Users/x/proj-a", "sessionId": "bbbb-2222",
         "timestamp": "2026-06-09T11:00:00Z",
         "message": {"role": "user", "content": "refactor the parser"}},
    ])
    _write_jsonl(proj_b / "cccc-3333.jsonl", [
        {"type": "user", "cwd": "/Users/x/proj-b", "sessionId": "cccc-3333",
         "timestamp": "2026-06-09T12:00:00Z",
         "message": {"role": "user", "content": "write docs"}},
    ])
    os.utime(proj_a / "bbbb-2222.jsonl", (1000, 1000))
    os.utime(proj_a / "aaaa-1111.jsonl", (2000, 2000))
    os.utime(proj_b / "cccc-3333.jsonl", (3000, 3000))
    return root


class TestTitlesAndPreviews:

    def test_rename_in_history_wins_as_title(self, claude_dir):
        from subagents.cc_sessions import find_cc_session
        _write_history(claude_dir, [
            ("aaaa-1111", "fix the login bug"),
            ("aaaa-1111", "/rename login-hardening"),
            ("aaaa-1111", "/rename auth-wave"),       # LAST rename wins
            ("aaaa-1111", "polish the error copy"),
        ])
        aaaa = find_cc_session("aaaa-1111", claude_dir=str(claude_dir))
        assert aaaa["title"] == "auth-wave"

    def test_history_last_prompt_is_preview_and_skips_junk(self, claude_dir):
        from subagents.cc_sessions import find_cc_session
        _write_history(claude_dir, [
            ("cccc-3333", "write docs"),
            ("cccc-3333", "tighten the intro"),
            ("cccc-3333", "<system-reminder>injected frame</system-reminder>"),
            ("cccc-3333", "[Runtime Context] stuff"),
            ("cccc-3333", "Caveat: the messages below were generated"),
            ("cccc-3333", "/compact"),
        ])
        cccc = find_cc_session("cccc-3333", claude_dir=str(claude_dir))
        assert cccc["preview"] == "tighten the intro"

    def test_custom_title_line_beats_auto_title(self, tmp_path):
        from subagents.cc_sessions import find_cc_session
        root = tmp_path / "claude-home"
        _write_jsonl(root / "projects" / "-x" / "dddd-4444.jsonl", [
            {"type": "ai-title", "aiTitle": "Review scope of slice 1",
             "sessionId": "dddd-4444"},
            {"type": "custom-title", "customTitle": "sprint-5-ish",
             "sessionId": "dddd-4444"},
            {"type": "user", "cwd": "/x", "sessionId": "dddd-4444",
             "message": {"role": "user", "content": "kick off slice 1"}},
        ])
        out = find_cc_session("dddd-4444", claude_dir=str(root))
        assert out["title"] == "sprint-5-ish"

    def test_auto_title_falls_back_when_no_rename(self, tmp_path):
        from subagents.cc_sessions import find_cc_session
        root = tmp_path / "claude-home"
        _write_jsonl(root / "projects" / "-x" / "eeee-5555.jsonl", [
            {"type": "summary", "summary": "Auto summary title",
             "sessionId": "eeee-5555"},
            {"type": "user", "cwd": "/x", "sessionId": "eeee-5555",
             "message": {"role": "user", "content": "do a thing"}},
        ])
        out = find_cc_session("eeee-5555", claude_dir=str(root))
        assert out["title"] == "Auto summary title"

    def test_junk_user_frames_skipped_for_first_prompt(self, tmp_path):
        from subagents.cc_sessions import find_cc_session
        root = tmp_path / "claude-home"
        _write_jsonl(root / "projects" / "-x" / "ffff-6666.jsonl", [
            {"type": "user", "cwd": "/x", "sessionId": "ffff-6666",
             "message": {"role": "user",
                         "content": "<system-reminder>boot context</system-reminder>"}},
            {"type": "user", "sessionId": "ffff-6666",
             "message": {"role": "user", "content": "[Runtime Context — injected]"}},
            {"type": "user", "sessionId": "ffff-6666",
             "message": {"role": "user", "content": "Caveat: generated frame"}},
            {"type": "user", "sessionId": "ffff-6666",
             "message": {"role": "user", "content": "the real first ask"}},
        ])
        out = find_cc_session("ffff-6666", claude_dir=str(root))
        assert out["first_prompt"] == "the real first ask"
        assert out["preview"] == "the real first ask"
        assert out["title"] == "the real first ask"

    def test_title_clipped_to_60(self, tmp_path):
        from subagents.cc_sessions import find_cc_session
        root = tmp_path / "claude-home"
        long = "x" * 200
        _write_jsonl(root / "projects" / "-x" / "gggg-7777.jsonl", [
            {"type": "user", "cwd": "/x", "sessionId": "gggg-7777",
             "message": {"role": "user", "content": long}},
        ])
        out = find_cc_session("gggg-7777", claude_dir=str(root))
        assert out["title"] == "x" * 60 + "…"

    def test_find_cc_session_carries_title(self, claude_dir):
        from subagents.cc_sessions import find_cc_session
        _write_history(claude_dir, [("bbbb-2222", "/rename parser-rework")])
        meta = find_cc_session("bbbb-2222", claude_dir=str(claude_dir))
        assert meta["title"] == "parser-rework"
        assert meta["cwd"] == "/Users/x/proj-a"


class TestFindSession:
    def test_find_by_id(self, claude_dir):
        from subagents.cc_sessions import find_cc_session
        meta = find_cc_session("bbbb-2222", claude_dir=str(claude_dir))
        assert meta is not None
        assert meta["cwd"] == "/Users/x/proj-a"
        assert meta["path"].endswith("bbbb-2222.jsonl")

    def test_find_missing_is_none(self, claude_dir):
        from subagents.cc_sessions import find_cc_session
        assert find_cc_session("zzzz", claude_dir=str(claude_dir)) is None
