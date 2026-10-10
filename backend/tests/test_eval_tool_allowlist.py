"""Regression tests for the disposable-evaluation tool allowlist."""

from __future__ import annotations

import pytest

from eval.tool_allowlist import allowlist_hash, eval_tool_allowlist
from tools.registry import ToolRegistry


def test_allowlist_requires_test_mode(monkeypatch):
    monkeypatch.setenv("CORSIA_EVAL_TOOL_ALLOWLIST", '["bridge_exec"]')
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)

    with pytest.raises(RuntimeError, match="CORSIA_TEST_MODE=1"):
        eval_tool_allowlist()


def test_allowlist_is_canonical_and_hashed(monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.setenv("CORSIA_EVAL_TOOL_ALLOWLIST", '["bridge_read","bridge_exec"]')

    names = eval_tool_allowlist()

    assert names == ("bridge_exec", "bridge_read")
    assert allowlist_hash(names) == allowlist_hash(("bridge_read", "bridge_exec"))


def test_registry_refuses_unknown_eval_tool():
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="missing"):
        registry.retain_only(("missing",))
