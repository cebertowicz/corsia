from __future__ import annotations

import pytest


@pytest.mark.parametrize("parts,total", (
    (("functi", "on"), 5), (("こんにち", "は"), 5),
    (("establi", "shment", " works"), 7),
))
def test_system_source_shares_preserve_total_when_tokenizer_merges_across_rows(parts, total):
    from core.effective_context import ContextSourceEntry
    from core.native_system_context import _retoken_native_system_components

    rows = [ContextSourceEntry(
        source_id=f"native:system:{index}", origin_section="system",
        label=str(index), stable_reference=f"test://{index}", freshness="fresh",
        inclusion="included", reason="test component", owner_surface="test",
        prompt_content=part,
    ) for index, part in enumerate(parts)]
    prompt = "".join(parts)
    measured = _retoken_native_system_components(rows, system_prompt=prompt, model="gpt-4")
    shares = [row.to_inventory_dict(model="gpt-4")["prompt_tokens"] for row in measured]
    assert all(value >= 0 for value in shares)
    assert sum(shares) == total
    assert "".join(row.prompt_content for row in measured) == prompt


@pytest.mark.parametrize("corrupt", (False, True))
def test_missing_custom_tokenizer_assets_do_not_download_or_disable_counting(tmp_path, corrupt):
    """An absent/corrupt optional cache must not fetch, erase data, or prevent counting."""
    import os
    import subprocess
    import sys

    asset = tmp_path / "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
    if corrupt:
        asset.write_bytes(b"corrupt cached asset")
    result = subprocess.run([sys.executable, "-c", """
import requests
def forbidden(*args, **kwargs):
    raise AssertionError('counter attempted to fetch an absent tokenizer asset')
requests.get = forbidden
from core.token_estimation import measure_text
for model in ('gpt-4', 'codex/gpt-5-codex'):
    result = measure_text('hello world', model=model)
    assert result.tokens > 0
    assert result.upper_bound is None
    assert result.tokenizer_identity == 'estimated:characters'
"""], env={**os.environ, "CUSTOM_TIKTOKEN_CACHE_DIR": str(tmp_path)},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "using labelled estimates" in result.stderr
    assert list(tmp_path.iterdir()) == ([asset] if corrupt else [])
    if corrupt:
        assert asset.read_bytes() == b"corrupt cached asset"


@pytest.mark.parametrize("text", (
    "こんにちは世界 🌍 café", '{"a":[1,2,3],"enabled":true}' * 100,
    "word " * 100, "<|endoftext|> is literal input",
))
def test_model_text_count_uses_installed_tokenizer_without_network(monkeypatch, text):
    import requests

    def no_network(*_args, **_kwargs):
        raise AssertionError("token counting attempted a download")

    monkeypatch.setattr(requests, "get", no_network)
    from core.token_estimation import measure_text
    from litellm.litellm_core_utils.default_encoding import encoding

    counted = measure_text(text, model="gpt-4")
    expected = len(encoding.encode(text, disallowed_special=()))
    assert counted.tokens == expected
    assert counted.upper_bound == expected
    assert counted.tokenizer_identity == "tiktoken:cl100k_base"


def test_unknown_alias_count_is_explicitly_estimated_without_download(monkeypatch):
    import tiktoken
    from core.token_estimation import _estimate_tokens, measure_text

    monkeypatch.setattr(tiktoken, "get_encoding", lambda *_a: pytest.fail("unknown tokenizer"))
    text = "unknown tokenizer material"
    measured = measure_text(text, model="local/custom-alias")
    assert measured.tokens == _estimate_tokens(text)
    assert measured.upper_bound is None
    assert measured.tokenizer_identity == "estimated:characters"


def test_o200k_text_count_uses_bundled_assets_without_network(monkeypatch):
    import requests
    import tiktoken
    from core.token_estimation import measure_text

    monkeypatch.setattr(requests, "get", lambda *_a, **_k: pytest.fail("tokenizer download"))
    text = "こんにちは世界 🌍 <|endoftext|>"
    result = measure_text(text, model="codex/gpt-5-codex")
    assert result.tokens == len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))
    assert result.tokenizer_identity == "tiktoken:o200k_base"


def test_request_does_not_strip_unknown_provider_into_a_known_model():
    from types import SimpleNamespace
    from core.token_estimation import measure_request

    target = SimpleNamespace(catalog_id="local", transport_model="local/gpt-4", expected_reported_model=None)
    result = measure_request("hello", target=target)
    assert result.tokenizer_identity == "estimated:characters"


def test_request_measurement_counts_tools_media_and_reasoning_without_mutation():
    from copy import deepcopy
    from types import SimpleNamespace
    from core.token_estimation import measure_request

    target = SimpleNamespace(catalog_id="selected", transport_model="gpt-4",
                             expected_reported_model=None)
    payload = {"messages": [{"role": "user", "content": "hello"}], "tools": []}
    before = deepcopy(payload)
    base = measure_request(payload, target=target)
    assert payload == before
    payload["messages"].append({"role": "assistant", "content": "answer",
                                "reasoning_content": "reasoning"})
    text = measure_request(payload, target=target)
    assert text.tokens > base.tokens
    payload["messages"].append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://invalid.example/image"}},
    ]})
    media = measure_request(payload, target=target)
    assert media.tokens > text.tokens
    payload["tools"] = [{"type": "function", "function": {
        "name": "read", "parameters": {"type": "object", "properties": {}},
    }}]
    assert measure_request(payload, target=target).tokens > media.tokens
    assert media.upper_bound is None  # Framing and media are still estimates.
    assert media.model == "selected"


def test_reasoning_content_is_counted_once_in_message_estimate():
    from core.token_estimation import _estimate_message_tokens, _estimate_tokens

    without_reasoning = {
        "role": "assistant",
        "content": "visible answer",
    }
    with_reasoning = {
        **without_reasoning,
        "reasoning_content": "private reasoning history",
    }

    assert (
        _estimate_message_tokens(with_reasoning)
        - _estimate_message_tokens(without_reasoning)
    ) == _estimate_tokens("private reasoning history")


def test_codex_input_and_output_text_blocks_are_counted():
    from core.token_estimation import _estimate_message_tokens, _estimate_tokens

    message = {
        "role": "assistant",
        "content": [
            {"type": "input_text", "text": "prior user input"},
            {"type": "output_text", "text": "prior assistant output"},
        ],
    }

    assert _estimate_message_tokens(message) == (
        4
        + _estimate_tokens("prior user input")
        + _estimate_tokens("prior assistant output")
    )


def test_claude_thinking_and_tool_use_blocks_are_counted():
    import json

    from core.token_estimation import _estimate_message_tokens, _estimate_tokens

    tool_input = {"path": "/tmp/example", "line": 7}
    message = {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "private Claude reasoning",
                "signature": "provider-private-signature",
            },
            {
                "type": "tool_use",
                "id": "toolu_123",
                "name": "Read",
                "input": tool_input,
            },
        ],
    }

    assert _estimate_message_tokens(message) == (
        4
        + _estimate_tokens("private Claude reasoning")
        + 4
        + _estimate_tokens("Read")
        + _estimate_tokens(json.dumps(tool_input, sort_keys=True, separators=(",", ":")))
    )


def test_claude_nested_tool_result_content_is_counted():
    from core.token_estimation import _estimate_message_tokens, _estimate_tokens

    message = {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_123",
            "content": [{"type": "text", "text": "complete tool output"}],
        }],
    }

    assert _estimate_message_tokens(message) == (
        4 + 4 + _estimate_tokens("complete tool output")
    )


def test_minified_json_structure_is_not_misclassified_as_dense_payload():
    import json

    from core.token_estimation import _estimate_tokens

    payload = [{
        "type": "function",
        "function": {
            "name": "wide_schema",
            "parameters": {
                "properties": {
                    f"field_{index}": {"type": "string"}
                    for index in range(40)
                }
            },
        },
    }]
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert len(rendered) > 80
    assert _estimate_tokens(rendered) == len(rendered) // 3


def test_minified_json_preserves_dense_string_floor():
    import json

    from core.token_estimation import _estimate_tokens

    dense = "A" * 500
    payload = [{
        "type": "function",
        "function": {
            "name": "dense_schema",
            "description": dense,
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    rendered = json.dumps(payload, separators=(",", ":"))

    assert _estimate_tokens(rendered) >= len(dense)
