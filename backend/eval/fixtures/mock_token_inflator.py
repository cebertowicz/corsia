
from __future__ import annotations

import contextlib
from typing import AsyncIterator
from unittest.mock import patch


@contextlib.asynccontextmanager
async def mock_token_inflator(
    *,
    multiplier: float = 10.0,
    fixed_tokens: int | None = None,
) -> AsyncIterator[dict]:
    from core import token_estimation

    real_estimate = token_estimation._estimate_tokens
    calls: list[tuple[int, int]] = []

    def fake_estimate(content) -> int:
        size = len(content) if isinstance(content, str) else 0
        if fixed_tokens is not None:
            calls.append((size, fixed_tokens))
            return fixed_tokens
        real_count = real_estimate(content)
        inflated = int(real_count * multiplier)
        calls.append((size, inflated))
        return inflated

    with patch.object(token_estimation, "_estimate_tokens", new=fake_estimate):
        yield {"calls": calls}
