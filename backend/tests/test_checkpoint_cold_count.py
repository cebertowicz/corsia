"""Cold checkpoint input is counted by its writer's tokenizer family (D1).

Calibration evidence: Turn & Delivery fold-floor work, D1 calibration. Claude 5
cold rows measured 1.281-1.663 provider tokens per o200k token; byte-level BPE
never exceeds one token per UTF-8 byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from llm.resolution import ExecutionTarget

PROSE = (
    "The checkpoint writer records durable decisions, open questions and the "
    "exact next action so the continuing model can resume without rereading. "
)


def _target(harness: str, model: str, *, window: int = 262_144) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{harness}/{model}",
        harness=harness,
        provider=harness,
        backend="harness",
        transport_model=model,
        expected_reported_model=None,
        context_window=window,
        available=True,
        availability_reason=None,
        display_name=model,
        supports_reasoning=True,
    )


CODEX = _target("codex", "gpt-5.6-terra", window=131_072)
CLAUDE = replace(
    _target("claude-code", "sonnet"), expected_reported_model="claude-sonnet-5",
)
LOCAL = _target("native", "omlx@ns-test/Qwen3.8-Flash-Next")


def _serialized(payload) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def _o200k(text: str) -> int:
    from core.token_estimation import _model_encoding

    return len(_model_encoding("gpt-5").encode(text, disallowed_special=()))


def _e0_identifiers() -> str:
    return " ".join(
        hashlib.sha256(str(index).encode()).hexdigest()[:8] for index in range(5000)
    )


def _cold_request(monkeypatch, target: ExecutionTarget, turns):
    from cache_warm_fixtures import make_exact_replay_selection
    from core.checkpoint_render import build_cold_checkpoint_admission

    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_a: {})
    monkeypatch.setattr(
        "prompts.checkpoint.build_checkpoint_system_prompt",
        lambda: "checkpoint instruction",
    )
    projection = make_exact_replay_selection("thread-1", turns).tail
    return build_cold_checkpoint_admission(
        checkpoint_id="checkpoint-1", thread_id="thread-1", trigger="pressure",
        source_target=target, writer_target=target,
        boundary=projection.sealed_boundary, previous=None,
        projection=projection, working_dir="/work/project",
        reasoning_effort=None, binary=target.harness, db=object(),
    ).provider_request


@pytest.mark.parametrize("target,surface,chunk", (
    (CODEX, 18_933, 150_000),
    (CLAUDE, 29_384, 300_000),
))
def test_prose_history_that_fits_the_writer_is_not_truncated(
    monkeypatch, target, surface, chunk,
):
    """Bytes refused history the writer can hold; the count must not."""
    from core.checkpoint_render import fit_cold_checkpoint_input

    body = (PROSE * (chunk // len(PROSE) + 1))[:chunk]
    request = _cold_request(monkeypatch, target, [
        [("user", "older ask"), ("assistant", body)],
        [("user", "newest ask"), ("assistant", body)],
    ])

    fitted = fit_cold_checkpoint_input(request, provider_surface_tokens=surface)

    assert fitted.first_included_seq == 1
    assert fitted.omitted_units == 0


def test_codex_point_release_counts_exact_o200k():
    from core.checkpoint_cold_count import cold_input_tokens

    payload = ({"role": "system", "content": "instruction"},
               {"role": "user", "content": PROSE * 40})

    assert cold_input_tokens(payload, CODEX) == _o200k(_serialized(payload))


def test_claude_count_uses_the_calibrated_ratio_below_bytes():
    from core.checkpoint_cold_count import cold_input_tokens

    payload = ({"role": "user", "content": PROSE * 40},)
    text = _serialized(payload)
    expected = -(-_o200k(text) * 5 // 3)

    assert cold_input_tokens(payload, CLAUDE) == expected
    assert expected < len(text.encode())


def test_claude_identifier_corpus_is_capped_by_bytes_not_undercounted():
    """E0: Haiku grew 29,094 tokens on 44,999 bytes of 8-hex identifiers."""
    from core.checkpoint_cold_count import cold_input_tokens

    corpus = _e0_identifiers()
    assert len(corpus.encode()) == 44_999
    counted = cold_input_tokens(corpus, CLAUDE)

    assert counted == len(_serialized(corpus).encode())
    assert counted >= 29_094


@pytest.mark.parametrize("target", (
    LOCAL,
    _target("native", "gpt-6-preview"),
    replace(CLAUDE, harness="native", transport_model="anthropic/claude-sonnet-5",
            expected_reported_model=None),
))
def test_family_selection_is_exact_bytes_or_claude(target):
    from core.checkpoint_cold_count import cold_input_tokens

    payload = {"text": PROSE * 10}
    size = len(_serialized(payload).encode())
    counted = cold_input_tokens(payload, target)

    if target.transport_model.startswith("anthropic/"):
        assert counted == -(-_o200k(_serialized(payload)) * 5 // 3)
    else:
        assert counted == size


def test_native_cold_loop_counts_like_its_admission():
    """The cold fork's per-call check uses the admission count, not chars/3."""
    from core.checkpoint_cold_count import cold_input_tokens
    from core.checkpoint_native_tools import input_fits
    from core.checkpoint_types import ProviderCheckpointRequest

    messages = [{"role": "user", "content": "约束🌍" * 4_000}]
    tools = [{"type": "function", "function": {"name": "noop"}}]
    counted = cold_input_tokens(messages, LOCAL) + cold_input_tokens(tools, LOCAL)
    request = ProviderCheckpointRequest(
        fork_id="checkpoint-1", thread_id="thread-1",
        target=replace(LOCAL, context_window=counted + 99),
        instruction="checkpoint", reasoning_effort=None, working_dir="/work",
        max_output_tokens=100,
    )

    assert input_fits(request, messages, tools) is False
    request = replace(request, target=replace(LOCAL, context_window=counted + 100))
    assert input_fits(request, messages, tools) is True


def test_send_time_room_counts_the_writer_family(isolated_db):
    """A cap that holds the instruction in real tokens must not reject the send."""
    from infra.database import get_db
    from infra.input_admission import _require_checkpoint_capacity
    from infra.send_types import SendRejected
    from prompts.checkpoint import build_checkpoint_system_prompt

    instruction = build_checkpoint_system_prompt()
    payload = ({"role": "system", "content": instruction},)
    needed = _o200k(_serialized(payload))
    window = needed + 16_384
    while window - 16_384 - window // 20 < needed:
        window += 1

    db = get_db()
    try:
        for model, provider, harness in (
            ("codex/gpt-5.6-terra", "codex", "codex"),
            ("omlx/qwen-test", "omlx", "native"),
        ):
            db.execute(
                "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
                "display_name,context_window,harness,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'t0','t0')",
                (model, provider, model.split("/")[-1], model, model, window, harness),
            )
        db.commit()

        _require_checkpoint_capacity(db, "codex/gpt-5.6-terra")
        with pytest.raises(SendRejected):
            _require_checkpoint_capacity(db, "omlx/qwen-test")
    finally:
        db.close()
