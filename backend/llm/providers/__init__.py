"""Provider-specific LLM modules.

Each module handles the quirks of one provider's API (Anthropic's cache_control
and thinking; OpenAI's Responses API and reasoning effort; local server API/base/key conventions for llama, LM
Studio, and oMLX). Importers should go through ``llm.router.chat_completion``
rather than calling these modules directly unless building the router itself.

``for_provider`` returns the network-server provider module for a given
provider key — call sites dispatch via this rather than per-provider
``import`` lines or ``startswith`` checks.
"""

from typing import Any


def for_provider(provider: str) -> Any:
    """Return the provider module implementing the network-server ops contract
    for the given provider key. Currently only ``"omlx"``.

    Raises ``ValueError`` for unknown keys — call sites should never invent a
    provider string; it comes from the ``network_servers.provider`` column.
    """
    if provider == "omlx":
        from llm.providers import omlx
        return omlx
    raise ValueError(f"No provider module for: {provider!r}")
