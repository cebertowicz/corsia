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


_OMLX_PREFILL_GUARD = (
    "litellm.BadRequestError: OpenAIException - oMLX prefill memory guard rejected "
    "this prompt: Prefill would require ~20.80 GB peak (current 18.80 GB + KV+SDPA "
    "2.00 GB) but the prefill limit is 20.75 GB (metal_cap ceiling 22.56 GB - "
    "safety margin 1.80 GB)"
)


def test_omlx_prefill_memory_guard_refusal_matches():
    assert is_provider_context_limit(_OMLX_PREFILL_GUARD)


def test_omlx_iogpu_wired_limit_refusal_matches():
    assert is_provider_context_limit(
        "oMLX prefill memory guard rejected this prompt: metal_cap ceiling is "
        "230.12 GB. Raise kernel iogpu.wired_limit_mb"
    )


def test_benign_memory_guard_mention_does_not_match():
    assert not is_provider_context_limit({"setting": "Memory Guard: aggressive"})
    assert not is_provider_context_limit("The memory guard protects the machine from prefill spikes.")
