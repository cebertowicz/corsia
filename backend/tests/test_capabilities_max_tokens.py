
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CORSIA_WORKSPACE", "/tmp/corsia-test-ws")

from llm.capabilities import get_model_max_output_tokens, _DEFAULT_MAX_OUTPUT_TOKENS_CAP  # noqa: E402


def _patch_catalog(catalog_row: dict | None):
    return patch.multiple(
        "llm.catalog",
        get_model=lambda _: catalog_row,
        get_model_by_litellm=lambda _: None,
    )


def test_stored_profile_max_tokens_is_not_read(isolated_db):
    """Output limits are the provider's: a saved profile value changes nothing."""
    from infra.database import get_db, init_model_profiles

    init_model_profiles()
    db = get_db()
    db.execute(
        "INSERT INTO model_profiles (model_id, max_tokens) VALUES (?, ?)",
        ("opus-4-7", 64000),
    )
    db.commit()
    db.close()
    with _patch_catalog({"id": "opus-4-7", "default_max_tokens": 16000}):
        assert get_model_max_output_tokens("anthropic/claude-opus-4-7", fallback=8192) == 16000


def test_catalog_default_caps_at_32k():
    with _patch_catalog({"id": "opus-4-7", "default_max_tokens": 128000}):
        assert (
            get_model_max_output_tokens("anthropic/claude-opus-4-7", fallback=8192)
            == _DEFAULT_MAX_OUTPUT_TOKENS_CAP
        )


def test_catalog_default_under_cap_is_used_as_is():
    with _patch_catalog({"id": "haiku-4-5", "default_max_tokens": 16000}):
        assert get_model_max_output_tokens("anthropic/claude-haiku-4-5", fallback=8192) == 16000


def test_unknown_model_returns_fallback():
    with _patch_catalog(None):
        assert get_model_max_output_tokens("openai/gpt-2-1959", fallback=8192) == 8192


def test_catalog_row_without_default_max_tokens_returns_fallback():
    with _patch_catalog({"id": "x", "default_max_tokens": None}):
        assert get_model_max_output_tokens("x", fallback=4096) == 4096
