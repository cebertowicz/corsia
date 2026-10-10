"""Tests for `hardcoded-model-name` rule."""

from __future__ import annotations


def test_anthropic_literal_in_default_fires(run_engine):
    """A model literal as the second arg of `defaults.get` fires."""
    src = (
        "def get_agent():\n"
        "    _fallback_model = defaults.get('model', 'anthropic/claude-haiku-4-5')\n"
    )
    findings = run_engine(
        "backend/core/agent.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1
    assert any("anthropic/claude-haiku-4-5" in f.message for f in findings)


def test_hosted_vllm_literal_as_module_constant_fires(run_engine):
    """`LOCAL_MODEL = 'hosted_vllm/...'` (the original sin) fires."""
    src = "LOCAL_MODEL = 'hosted_vllm/Qwen3.5-35B-A3B'\n"
    findings = run_engine(
        "backend/runtime.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1
    assert any("hosted_vllm/Qwen3.5-35B-A3B" in f.message for f in findings)


def test_openai_gpt_literal_fires(run_engine):
    """`openai/gpt-5-mini` as a fallback fires too."""
    src = "FALLBACK = 'openai/gpt-5-mini'\n"
    findings = run_engine(
        "backend/llm/router.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1


def test_lmstudio_literal_fires(run_engine):
    """`lmstudio/` prefix also fires."""
    src = "DEV_MODEL = 'lmstudio/llama-3.3-70b-instruct'\n"
    findings = run_engine(
        "backend/dev_helpers.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1


def test_catalog_file_does_not_fire(run_engine):
    """Catalog data files are allow-listed (their job is to know model IDs)."""
    src = (
        "MODELS = [\n"
        "    {'id': 'anthropic-claude-haiku', 'litellm_model': 'anthropic/claude-haiku-4-5'},\n"
        "    {'id': 'openai-gpt5-mini', 'litellm_model': 'openai/gpt-5-mini'},\n"
        "]\n"
    )
    findings = run_engine(
        "backend/llm/catalog.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []


def test_pricing_file_does_not_fire(run_engine):
    """Pricing data files are allow-listed."""
    src = (
        "PRICING = {\n"
        "    'anthropic/claude-haiku-4-5': {'input': 1.0, 'output': 5.0},\n"
        "    'openai/gpt-5-mini': {'input': 0.15, 'output': 0.6},\n"
        "}\n"
    )
    findings = run_engine(
        "backend/llm/pricing.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []


def test_capabilities_file_does_not_fire(run_engine):
    """Capabilities data files are allow-listed."""
    src = "CAPS = {'anthropic/claude-opus-4-7': {'supports_vision': True}}\n"
    findings = run_engine(
        "backend/llm/capabilities.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []


def test_schema_ddl_default_model_fires(run_engine):
    """SQL DEFAULT clauses naming a model FIRE (issue #317).

    A `judge_model TEXT NOT NULL DEFAULT 'anthropic/...'` is a model the
    user did not choose — it IS the anti-pattern, not "a data choice". The
    schema.py exclusion was removed; this must now be flagged.
    """
    src = (
        "SCHEMA = '''\n"
        "CREATE TABLE settings (\n"
        "    judge_model TEXT NOT NULL DEFAULT 'anthropic/claude-sonnet-4-6'\n"
        ");\n"
        "'''\n"
    )
    findings = run_engine(
        "backend/infra/schema.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1
    assert any("anthropic/claude-sonnet-4-6" in f.message for f in findings)


def test_config_module_model_literal_fires(run_engine):
    """A model literal in a config module FIRES (issue #317).

    Config must read env/settings, never hold a model literal. This rule
    only ever consulted `data_file_globs`, never `config_path_globs`, so
    config.py was never actually excluded — assert that explicitly.
    """
    src = "LOCAL_MODEL = 'hosted_vllm/Qwen3.6-35B-A3B'\n"
    findings = run_engine(
        "backend/infra/config.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1


def test_omlx_local_model_literal_fires(run_engine):
    """`oMLX/<model>` literal defaults FIRE — local defaults are the same bug."""
    src = "JUDGE = 'oMLX/qwen3.6-35B-A3B'\n"
    findings = run_engine(
        "backend/eval/scorers.py", src, rule_id="hardcoded-model-name"
    )
    assert len(findings) >= 1


def test_test_file_does_not_fire(run_engine):
    """Test files legitimately pin specific models for reproducibility."""
    src = (
        "def test_inference():\n"
        "    response = call_model('anthropic/claude-haiku-4-5', prompt='hi')\n"
    )
    findings = run_engine(
        "backend/tests/test_inference.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []


def test_model_in_docstring_does_not_fire(run_engine):
    """Model names mentioned in docstrings are documentation, not defaults."""
    src = (
        '"""Example usage::\n'
        "\n"
        "    call_model('anthropic/claude-haiku-4-5')\n"
        '"""\n'
        "\n"
        "def call_model(name: str): pass\n"
    )
    findings = run_engine(
        "backend/llm/api.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []


def test_non_provider_slash_string_does_not_fire(run_engine):
    """Random `slash/separated` strings that aren't provider/model don't fire."""
    src = (
        "PATH_KEY = 'memory/store'\n"
        "ROUTE = 'api/v1/users'\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []


def test_local_backends_provider_prefix_does_not_fire(run_engine):
    """`local_backends.py` declares provider prefixes — allow-listed."""
    src = (
        "class LMStudioBackend:\n"
        "    _prefix = 'lmstudio/'\n"
    )
    findings = run_engine(
        "backend/llm/local_backends.py", src, rule_id="hardcoded-model-name"
    )
    assert findings == []
