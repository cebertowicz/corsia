
from __future__ import annotations

from .blank_session import blank_session
from .mock_llm_replies import mock_llm_replies
from .stub_filesystem import stub_filesystem
from .stub_workspace import stub_workspace
from .mock_cost_estimator import mock_cost_estimator
from .mock_token_inflator import mock_token_inflator
from .mock_model_context_window import mock_model_context_window


FIXTURES: dict[str, callable] = {
    "blank_session": blank_session,
    "mock_llm_replies": mock_llm_replies,
    "stub_filesystem": stub_filesystem,
    "stub_workspace": stub_workspace,
    "mock_cost_estimator": mock_cost_estimator,
    "mock_token_inflator": mock_token_inflator,
    "mock_model_context_window": mock_model_context_window,
}


__all__ = [
    "FIXTURES",
    "blank_session",
    "mock_llm_replies",
    "stub_filesystem",
    "stub_workspace",
    "mock_cost_estimator",
    "mock_token_inflator",
    "mock_model_context_window",
]
