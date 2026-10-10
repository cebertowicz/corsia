from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from subagents.provider_context_limits import is_provider_context_limit  # noqa: E402


def test_context_window_exceeded_message_matches():
    assert is_provider_context_limit("Codex turn failed: contextWindowExceeded")


def test_ran_out_of_room_message_matches():
    assert is_provider_context_limit(
        "Codex ran out of room in the model's context window."
    )


def test_prompt_too_long_message_matches():
    assert is_provider_context_limit({"result": "Prompt is too long"})


def test_unrelated_context_window_mention_does_not_match():
    """A benign message that merely mentions 'context window' must not classify
    as a context-limit error (the bare-substring false positive this narrowed
    the marker list to fix)."""
    assert not is_provider_context_limit(
        "model switch reduced context window from 262144 to 200000 tokens"
    )


def test_unrelated_maximum_context_mention_does_not_match():
    assert not is_provider_context_limit(
        {"message": "Model has a maximum context length of 128000 tokens."}
    )


def test_none_and_empty_do_not_match():
    assert not is_provider_context_limit(None)
    assert not is_provider_context_limit("")
    assert not is_provider_context_limit({})
