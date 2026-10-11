"""Provider display metadata for the settings API."""

from __future__ import annotations

# ``hidden`` entries stay addressable by name (PUT/DELETE/test still resolve, so
# a stale saved key can still be cleared) but are omitted from the provider list
# the settings UI renders. openrouter/deepseek/gemini/groq are hidden because
# nothing behind them works: ``llm.catalog_cloud.discover_cloud_models`` only
# implements openai + anthropic, ``llm.capabilities._CLOUD_PROVIDERS`` only
# carries the ``anthropic/`` + ``openai/`` prefixes, and ``llm.router`` has no
# dispatch leg for them — a model with one of those prefixes would fall through
# to ``base_completion`` with no ``api_base`` and hit the poisoned-global 404
# The supported way to reach an OpenAI-compatible cloud endpoint is the External
# provider (``llm.external_provider``): base URL + token + wire dialect.

PROVIDER_META: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "type": "cloud",
        "test_url": "https://api.anthropic.com/v1/messages",
        "docs_url": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "label": "OpenAI",
        "type": "cloud",
        "test_url": "https://api.openai.com/v1/models",
        "docs_url": "https://platform.openai.com/api-keys",
    },
    "openrouter": {
        "hidden": True,
        "label": "OpenRouter",
        "type": "cloud",
        "test_url": "https://openrouter.ai/api/v1/models",
        "docs_url": "https://openrouter.ai/keys",
    },
    "deepseek": {
        "hidden": True,
        "label": "DeepSeek",
        "type": "cloud",
        "test_url": "https://api.deepseek.com/v1/models",
        "docs_url": "https://platform.deepseek.com/api_keys",
    },
    "gemini": {
        "hidden": True,
        "label": "Google Gemini",
        "type": "cloud",
        "test_url": None,
        "docs_url": "https://aistudio.google.com/apikey",
    },
    "groq": {
        "hidden": True,
        "label": "Groq",
        "type": "cloud",
        "test_url": "https://api.groq.com/openai/v1/models",
        "docs_url": "https://console.groq.com/keys",
    },
    "omlx": {
        "label": "oMLX (Network)",
        "type": "network",
        "test_url": None,
        "docs_url": None,
        "capabilities": [
            "multi_model",
            "multi_server",
            "discovery",
            "remote_management",
            "vision",
        ],
    },
}
