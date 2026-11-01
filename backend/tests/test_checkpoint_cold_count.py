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
    from core.checkpoint_cold_admission import build_checkpoint_admission

    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_a: {})
    monkeypatch.setattr(
        "prompts.checkpoint.build_checkpoint_system_prompt",
        lambda: "checkpoint instruction",
    )
    projection = make_exact_replay_selection("thread-1", turns).tail
    return build_checkpoint_admission(
        checkpoint_id="checkpoint-1", thread_id="thread-1", trigger="pressure",
        source_target=target, writer_target=target,
        boundary=projection.sealed_boundary, previous=None,
        projection=projection, working_dir="/work/project",
        reasoning_effort=None, binary=target.harness, carried_tokens=0, db=object(),
    ).provider_request


@pytest.mark.parametrize("target,surface,chunk", (
    (CODEX, 18_933, 150_000),
    (CLAUDE, 29_384, 300_000),
))
def test_prose_history_that_fits_the_writer_is_not_truncated(
    monkeypatch, target, surface, chunk,
):
    """Bytes refused history the writer can hold; the count must not."""
    from core.fork_render import fit_cold_fork_input

    body = (PROSE * (chunk // len(PROSE) + 1))[:chunk]
    request = _cold_request(monkeypatch, target, [
        [("user", "older ask"), ("assistant", body)],
        [("user", "newest ask"), ("assistant", body)],
    ])

    fitted = fit_cold_fork_input(request, provider_surface_tokens=surface)

    assert fitted.first_included_seq == 1
    assert fitted.omitted_units == 0


def test_codex_point_release_counts_exact_o200k():
    from core.fork_cold_count import cold_input_tokens

    payload = ({"role": "system", "content": "instruction"},
               {"role": "user", "content": PROSE * 40})

    assert cold_input_tokens(payload, CODEX) == _o200k(_serialized(payload))


def test_claude_count_uses_the_calibrated_ratio_below_bytes():
    from core.fork_cold_count import cold_input_tokens

    payload = ({"role": "user", "content": PROSE * 40},)
    text = _serialized(payload)
    expected = -(-_o200k(text) * 5 // 3)

    assert cold_input_tokens(payload, CLAUDE) == expected
    assert expected < len(text.encode())


def test_claude_identifier_corpus_is_capped_by_bytes_not_undercounted():
    """E0: Haiku grew 29,094 tokens on 44,999 bytes of 8-hex identifiers."""
    from core.fork_cold_count import cold_input_tokens

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
    from core.fork_cold_count import cold_input_tokens

    payload = {"text": PROSE * 10}
    size = len(_serialized(payload).encode())
    counted = cold_input_tokens(payload, target)

    if target.transport_model.startswith("anthropic/"):
        assert counted == -(-_o200k(_serialized(payload)) * 5 // 3)
    else:
        assert counted == size


class _Backend:
    def __init__(self, params=None, error=None):
        self.params, self.error, self.asked = params, error, []

    def make_provider_params(self, model):
        self.asked.append(model)
        if self.error is not None:
            raise self.error
        return self.params


def _serve(monkeypatch, backend, reply):
    """Route local writers to ``backend`` and answer /tokenize with ``reply``."""
    import httpx

    posts = []

    def post(url, *, json, headers, timeout):
        posts.append((url, json, headers, timeout))
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(
        "llm.local_backends.find_backend",
        lambda model: backend if model.startswith("omlx@") else None,
    )
    monkeypatch.setattr(httpx, "post", post)
    return posts


def test_local_writer_counts_with_its_own_servers_tokenizer(monkeypatch):
    """A local model's own tokenizer counts its cold input, not UTF-8 bytes."""
    from core.fork_cold_count import cold_input_tokens

    backend = _Backend({
        "model": "openai/Qwen3.8-Flash-Next", "api_base": "http://127.0.0.1:8210/v1",
        "api_key": "local-key",
    })
    posts = _serve(monkeypatch, backend, {"count": 3_610, "tokens": []})
    payload = ({"role": "user", "content": PROSE * 100},)

    counted = cold_input_tokens(payload, LOCAL)

    assert counted == 3_610 < len(_serialized(payload).encode())
    assert backend.asked == ["omlx@ns-test/Qwen3.8-Flash-Next"]
    url, body, headers, _timeout = posts[0]
    assert url == "http://127.0.0.1:8210/tokenize"
    assert body == {"model": "Qwen3.8-Flash-Next", "prompt": _serialized(payload)}
    assert headers == {"Authorization": "Bearer local-key"}


@pytest.mark.parametrize("reply", (
    {"error": "not found"},
    {"count": "many"},
    ["not", "a", "body"],
))
def test_local_server_without_a_token_count_keeps_bytes(monkeypatch, reply):
    from core.fork_cold_count import cold_input_tokens

    backend = _Backend({"model": "openai/qwen", "api_base": "http://127.0.0.1:11434/v1"})
    _serve(monkeypatch, backend, reply)
    payload = {"text": PROSE * 10}

    assert cold_input_tokens(payload, LOCAL) == len(_serialized(payload).encode())


def test_unreachable_or_refusing_local_server_keeps_bytes(monkeypatch):
    import httpx

    from core.fork_cold_count import cold_input_tokens

    payload = {"text": PROSE * 10}
    size = len(_serialized(payload).encode())
    backend = _Backend({"model": "openai/qwen", "api_base": "http://127.0.0.1:1234/v1"})
    _serve(monkeypatch, backend, httpx.ConnectError("refused"))
    assert cold_input_tokens(payload, LOCAL) == size

    def not_found(url, *, json, headers, timeout):
        return httpx.Response(404, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", not_found)
    assert cold_input_tokens(payload, LOCAL) == size

    stale = _Backend(error=ValueError("oMLX server has no reachable endpoint: ns-test"))
    posts = _serve(monkeypatch, stale, {"count": 1})
    assert cold_input_tokens(payload, LOCAL) == size
    assert posts == []


def test_only_an_openai_compatible_execution_target_is_asked(monkeypatch):
    """The send-time cap check passes a bare model id and must not call out."""
    from core.fork_cold_count import cold_input_tokens

    payload = {"text": PROSE * 10}
    size = len(_serialized(payload).encode())
    anthropic = _Backend({"model": "anthropic/qwen", "api_base": "http://127.0.0.1:9000"})
    posts = _serve(monkeypatch, anthropic, {"count": 1})

    assert cold_input_tokens(payload, LOCAL) == size
    assert cold_input_tokens(payload, "omlx@ns-test/Qwen3.8-Flash-Next") == size
    assert cold_input_tokens(payload, CODEX) == _o200k(_serialized(payload))
    assert posts == []
    assert anthropic.asked == ["omlx@ns-test/Qwen3.8-Flash-Next"]


def test_native_cold_loop_counts_like_its_admission():
    """The cold fork's per-call check uses the admission count, not chars/3."""
    from core.fork_cold_count import cold_input_tokens
    from core.fork_native_tools import input_fits
    from core.checkpoint_types import CheckpointProviderForkRequest

    messages = [{"role": "user", "content": "约束🌍" * 4_000}]
    tools = [{"type": "function", "function": {"name": "noop"}}]
    counted = cold_input_tokens(messages, LOCAL) + cold_input_tokens(tools, LOCAL)
    request = CheckpointProviderForkRequest(
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
