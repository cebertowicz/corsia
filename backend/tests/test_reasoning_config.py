
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.reasoning_config import reasoning_config_for  # noqa: E402




def test_openai_o1_returns_effort_levels():
    rc = reasoning_config_for("openai", "o1")
    assert rc is not None
    assert rc["type"] == "effort_levels"
    assert "low" in rc["values"]
    assert "high" in rc["values"]
    assert rc["default"] in rc["values"]


def test_openai_o3_mini_returns_effort_levels():
    rc = reasoning_config_for("openai", "o3-mini")
    assert rc["type"] == "effort_levels"


def test_openai_gpt5_returns_effort_levels():
    rc = reasoning_config_for("openai", "gpt-5")
    assert rc["type"] == "effort_levels"
    assert rc["values"] == ["minimal", "low", "medium", "high"]


def test_openai_gpt5_nano_returns_effort_levels():
    rc = reasoning_config_for("openai", "gpt-5-nano")
    assert rc["type"] == "effort_levels"
    assert "xhigh" not in rc["values"]


def test_openai_gpt5_4_includes_xhigh():
    rc = reasoning_config_for("openai", "gpt-5.4")
    assert rc["type"] == "effort_levels"
    assert "xhigh" in rc["values"]


def test_openai_gpt5_5_includes_xhigh_and_minimal():
    rc = reasoning_config_for("openai", "gpt-5.5")
    assert rc["type"] == "effort_levels"
    assert "xhigh" in rc["values"]
    assert "minimal" in rc["values"]


def test_openai_o1_no_minimal_no_xhigh():
    rc = reasoning_config_for("openai", "o1")
    assert rc["type"] == "effort_levels"
    assert rc["values"] == ["low", "medium", "high"]


def test_openai_o3_mini_no_minimal_no_xhigh():
    rc = reasoning_config_for("openai", "o3-mini")
    assert rc["values"] == ["low", "medium", "high"]


def test_openai_gpt4o_returns_none():
    assert reasoning_config_for("openai", "gpt-4o") is None


def test_openai_gpt35_returns_none():
    assert reasoning_config_for("openai", "gpt-3.5-turbo") is None


def test_anthropic_opus_47_returns_effort_levels_with_xhigh():
    rc = reasoning_config_for("anthropic", "claude-opus-4-7")
    assert rc is not None
    assert rc["type"] == "effort_levels"
    assert rc["values"] == ["low", "medium", "high", "xhigh", "max"]
    assert rc["default"] in rc["values"]


def test_anthropic_opus_46_returns_effort_levels_no_xhigh():
    rc = reasoning_config_for("anthropic", "claude-opus-4-6")
    assert rc is not None
    assert rc["type"] == "effort_levels"
    assert rc["values"] == ["low", "medium", "high", "max"]
    assert "xhigh" not in rc["values"]


def test_anthropic_sonnet_46_returns_effort_levels_no_xhigh():
    rc = reasoning_config_for("anthropic", "claude-sonnet-4-6")
    assert rc["type"] == "effort_levels"
    assert rc["values"] == ["low", "medium", "high", "max"]


def test_anthropic_sonnet_47_still_token_budget():
    rc = reasoning_config_for("anthropic", "claude-sonnet-4-7")
    assert rc["type"] == "token_budget"


def test_anthropic_haiku_returns_token_budget():
    rc = reasoning_config_for("anthropic", "claude-haiku-4-5")
    assert rc["type"] == "token_budget"


def test_anthropic_haiku_46_returns_token_budget():
    rc = reasoning_config_for("anthropic", "claude-haiku-4-6")
    assert rc["type"] == "token_budget"


def test_anthropic_old_sonnet_returns_none():
    assert reasoning_config_for("anthropic", "claude-3-5-sonnet-20241022") is None




def test_local_model_name_alone_yields_no_shape():
    """A model's name is not evidence of its capability.

    Local shapes come from what the server reported at discovery and are stored
    on the catalog row; a model with no such row resolves to None however
    suggestive its name is.
    """
    assert reasoning_config_for("omlx", "qwen3-synthetic-8b") is None


def test_other_local_model_name_alone_yields_no_shape():
    assert reasoning_config_for("external", "deepseek-r1-synthetic-7b") is None


def test_unknown_provider_returns_none():
    assert reasoning_config_for("acme", "local-model-8b") is None




def test_empty_model_id_returns_none():
    assert reasoning_config_for("openai", "") is None


def test_unknown_provider_returns_none():
    assert reasoning_config_for("coherence-mistral", "o1") is None


def test_local_model_through_omlx_provider_has_no_name_matcher():
    assert reasoning_config_for("omlx", "qwen3-synthetic-30b") is None


def test_catalog_metadata_wins_over_matcher(monkeypatch):
    import json as _json

    class _FakeRow:
        def __init__(self, blob): self._blob = blob
        def __getitem__(self, k): return self._blob if k == "metadata_json" else None

    class _FakeDB:
        def __init__(self, blob): self._blob = blob
        def execute(self, *a, **kw):
            class _Cursor:
                def __init__(self, blob): self._blob = blob
                def fetchone(self): return _FakeRow(self._blob)
            return _Cursor(self._blob)
        def close(self): pass

    catalog_payload = {
        "reasoning_shape": {"type": "effort_levels", "values": ["x", "y"], "default": "y"},
        "input_cost_per_1m": 1.23,
    }
    monkeypatch.setattr(
        "infra.database.get_db",
        lambda: _FakeDB(_json.dumps(catalog_payload)),
    )

    rc = reasoning_config_for("anthropic", "claude-sonnet-4-6")
    assert rc == {"type": "effort_levels", "values": ["x", "y"], "default": "y"}


def test_catalog_lookup_failure_is_not_reported_as_no_reasoning(monkeypatch):
    """A failed capability read must surface, not masquerade as "no support".

    Local shapes come only from the catalog, so a swallowed database error
    would silently strip a model's reasoning control. The error propagates to
    the caller, which already depends on the same database.
    """
    import pytest

    def _raises():
        raise RuntimeError("DB unavailable")
    monkeypatch.setattr("infra.database.get_db", _raises)

    with pytest.raises(RuntimeError, match="DB unavailable"):
        reasoning_config_for("omlx", "qwen38")
    with pytest.raises(RuntimeError, match="DB unavailable"):
        reasoning_config_for("openai", "o1")


def test_unreadable_catalog_metadata_is_a_data_failure(monkeypatch):
    """Corrupt metadata must surface like every other catalog read, not vanish."""
    import pytest

    class _FakeRow:
        def __getitem__(self, k): return "{not json" if k == "metadata_json" else None

    class _FakeDB:
        def execute(self, *a, **kw):
            class _Cursor:
                def fetchone(self): return _FakeRow()
            return _Cursor()
        def close(self): pass

    monkeypatch.setattr("infra.database.get_db", lambda: _FakeDB())
    with pytest.raises(ValueError):
        reasoning_config_for("omlx", "qwen38")


_QWEN38 = {"type": "effort_levels", "values": ["xhigh", "medium", "low"], "default": "xhigh"}


def test_effective_effort_keeps_a_level_the_model_accepts(monkeypatch):
    from llm import reasoning_config as rc
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda m: _QWEN38)
    assert rc.effective_reasoning_effort("medium", "qwen38") == "medium"
    assert rc.effective_reasoning_effort(" XHIGH ", "qwen38") == "xhigh"


def test_effective_effort_resolves_a_stale_level_to_what_the_picker_shows(monkeypatch):
    """A thread that ran DeepSeek at 'high' and now uses Qwen 3.8 gets Qwen's default."""
    from llm import reasoning_config as rc
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda m: _QWEN38)
    assert rc.effective_reasoning_effort("high", "qwen38") == "xhigh"


def test_effective_effort_leaves_off_auto_and_unset_alone(monkeypatch):
    from llm import reasoning_config as rc
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda m: _QWEN38)
    assert rc.effective_reasoning_effort(None, "qwen38") is None
    assert rc.effective_reasoning_effort("", "qwen38") is None
    assert rc.effective_reasoning_effort("none", "qwen38") == "none"
    assert rc.effective_reasoning_effort("auto", "qwen38") == "auto"


def test_effective_effort_passes_through_when_the_vocabulary_is_unknown(monkeypatch):
    from llm import reasoning_config as rc
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda m: None)
    assert rc.effective_reasoning_effort("high", "unknown") == "high"
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda m: {"type": "boolean", "default": True})
    assert rc.effective_reasoning_effort("medium", "qwen36") == "medium"
