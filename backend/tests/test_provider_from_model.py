
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from llm.capabilities import provider_from_model  # noqa: E402


class TestProviderFromModel:

    def test_anthropic(self):
        assert provider_from_model("anthropic/claude-sonnet-4-6") == "anthropic"

    def test_anthropic_opus(self):
        assert provider_from_model("anthropic/claude-opus-4-7") == "anthropic"

    def test_openai(self):
        assert provider_from_model("openai/gpt-4o") == "openai"

    def test_openai_o1(self):
        assert provider_from_model("openai/o3-mini") == "openai"

    def test_external_simple(self):
        assert provider_from_model("external@ns-1/qwen2.5-72b") == "external"

    def test_external_with_slash_in_model(self):
        assert provider_from_model("external@ns-1/mlx-community/Model-8B") == "external"

    def test_omlx_simple(self):
        assert provider_from_model("omlx@ns-1/qwen3.5-vl-7b-mlx") == "omlx"

    def test_omlx_with_slash_in_model(self):
        assert provider_from_model("omlx@ns-1/mlx-community/Model-VL-7B-4bit") == "omlx"

    def test_gemini(self):
        assert provider_from_model("gemini/gemini-2.0-flash") == "gemini"

    def test_empty_string(self):
        assert provider_from_model("") == "unknown"

    def test_no_slash(self):
        assert provider_from_model("barestring") == "unknown"

    def test_none(self):
        assert provider_from_model(None) == "unknown"  # type: ignore[arg-type]

    def test_opus_47_dated_snapshot(self):
        assert provider_from_model("anthropic/claude-opus-4-7-20260205") == "anthropic"

    def test_opus_47_prefix_match_does_not_false_positive(self):
        assert provider_from_model("anthropic/claude-opus-4-6") == "anthropic"
        assert provider_from_model("anthropic/claude-opus-4-7") == "anthropic"
