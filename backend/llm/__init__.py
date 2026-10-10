"""LLM subsystem — routing, transport, provider modules, local-server lifecycle.

Public surface is intentionally narrow: `chat_completion` is the only thing
most callers need, and it lives at `llm.router`. Types (`LLMResponse`,
`ToolCallRequest`) and internal helpers (`_is_transient_error`) are reached
through their defining modules by the handful of callers that need them —
keeping them out of the package surface avoids pretending they're part of
the external API.

Sub-packages:
    llm.providers.*     — per-provider modules (anthropic, openai, llama, …)
    llm.local_backends  — LocalModelBackend registry for local servers
    llm.lifecycle       — start/stop/health of local server processes
    llm.reasoning_config — capability descriptor per (provider, model)
    llm.base            — shared transport (sanitize, stream, retry, parse)
    llm.router          — chat_completion dispatch + shared types
"""

from __future__ import annotations

from llm.router import chat_completion  # noqa: F401

__all__ = ["chat_completion"]
