
from __future__ import annotations

import contextlib
from typing import AsyncIterator
from unittest.mock import patch


@contextlib.asynccontextmanager
async def mock_model_context_window(
    *,
    window_tokens: int,
    model_filter: str | None = None,
) -> AsyncIterator[dict]:
    from llm import capabilities as caps

    real_fn = caps.get_model_context_window
    calls: list[tuple[str, int]] = []

    def fake_window(model: str) -> int:
        if model_filter and model_filter not in model:
            real = real_fn(model)
            calls.append((model, real))
            return real
        calls.append((model, window_tokens))
        return window_tokens

    with patch.object(caps, "get_model_context_window", new=fake_window):
        yield {"calls": calls}
