
from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator
from unittest.mock import patch


@contextlib.asynccontextmanager
async def mock_cost_estimator(
    *,
    cost_usd_per_call: float = 0.01,
    fixed_response: dict[str, Any] | None = None,
) -> AsyncIterator[dict]:
    calls: list[tuple[str, dict]] = []

    def fake_calculate_cost(model: str, usage: dict | None = None) -> float | dict:
        calls.append((model, dict(usage or {})))
        if fixed_response is not None:
            return fixed_response
        return cost_usd_per_call

    with patch("llm.pricing.calculate_cost", new=fake_calculate_cost):
        yield {"calls": calls}
