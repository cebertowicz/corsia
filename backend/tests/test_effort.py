"""Tests for infra.effort — resolution, clamping, and defaults."""

from __future__ import annotations

import json
import os
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from tests.vendor_account_fixtures import signed_in_default  # noqa: E402


def _config_values(cmd: list[str]) -> list[str]:
    return [cmd[index + 1] for index, arg in enumerate(cmd[:-1]) if arg == "-c"]


class TestClampEffort:
    def test_valid_cc_values_pass_through(self):
        from infra.effort import clamp_effort
        for v in ("low", "medium", "high", "xhigh", "max"):
            assert clamp_effort(v, "cc") == v

    def test_valid_codex_values_pass_through(self):
        from infra.effort import clamp_effort
        for v in ("none", "minimal", "low", "medium", "high", "xhigh"):
            assert clamp_effort(v, "codex") == v

    def test_cc_clamps_none_to_low(self):
        from infra.effort import clamp_effort
        assert clamp_effort("none", "cc") == "low"

    def test_cc_clamps_minimal_to_low(self):
        from infra.effort import clamp_effort
        assert clamp_effort("minimal", "cc") == "low"

    def test_codex_clamps_max_to_xhigh(self):
        from infra.effort import clamp_effort
        assert clamp_effort("max", "codex") == "xhigh"

    def test_unknown_value_falls_back_to_high(self):
        from infra.effort import clamp_effort
        assert clamp_effort("turbo", "cc") == "high"
        assert clamp_effort("turbo", "codex") == "high"

    def test_cc_max_is_valid(self):
        from infra.effort import clamp_effort
        assert clamp_effort("max", "cc") == "max"

    def test_codex_none_is_valid(self):
        from infra.effort import clamp_effort
        assert clamp_effort("none", "codex") == "none"


class TestResolveEffort:
    def test_explicit_value_wins(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {"cc": "low"})
        assert e.resolve_effort("xhigh", "cc") == "xhigh"

    def test_explicit_value_is_clamped(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        # "none" is invalid for cc → clamped to "low"
        assert e.resolve_effort("none", "cc") == "low"

    def test_configured_default_beats_seed(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {"cc": "medium"})
        monkeypatch.setattr(e, "_seed_effort_defaults", lambda: {"cc": "high"})
        assert e.resolve_effort(None, "cc") == "medium"

    def test_seed_default_used_when_no_configured(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        monkeypatch.setattr(e, "_seed_effort_defaults", lambda: {"cc": "xhigh"})
        assert e.resolve_effort(None, "cc") == "xhigh"

    def test_falls_back_to_high_when_no_seed(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        monkeypatch.setattr(e, "_seed_effort_defaults", lambda: {})
        assert e.resolve_effort(None, "cc") == "high"

    def test_codex_explicit_max_clamped(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        assert e.resolve_effort("max", "codex") == "xhigh"

    def test_whitespace_stripped_from_explicit(self, monkeypatch):
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        assert e.resolve_effort("  high  ", "cc") == "high"


class TestSeedFile:
    def test_delegation_defaults_json_has_effort_defaults(self):
        from pathlib import Path
        seed_path = Path(_BACKEND_DIR) / "infra" / "delegation_defaults.json"
        data = json.loads(seed_path.read_text())
        assert "effort_defaults" in data
        defaults = data["effort_defaults"]
        assert defaults.get("cc") == "high"
        assert defaults.get("codex") == "high"

    def test_seed_effort_defaults_returns_both_runtimes(self):
        from infra.effort import _seed_effort_defaults
        defaults = _seed_effort_defaults()
        assert defaults.get("cc") == "high"
        assert defaults.get("codex") == "high"


class TestCCStreamEffortFlag:
    def _make(self, effort=None, **kw):
        from subagents.cc_stream import CCStreamSession
        kw.setdefault("binary", "claude")
        kw.setdefault("session_id", "s1")
        return CCStreamSession(effort=effort, **kw)

    def test_effort_flag_present_in_cmd(self):
        cmd = self._make(effort="high").build_cmd()
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_no_effort_no_flag(self):
        cmd = self._make(effort=None).build_cmd()
        assert "--effort" not in cmd

    def test_effort_xhigh(self):
        cmd = self._make(effort="xhigh").build_cmd()
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "xhigh"


class TestCodexAppServerEffortFlag:
    def _make(self, effort=None, **kw):
        from subagents.codex_app_server import CodexAppServerSession
        kw.setdefault("binary", "codex")
        kw.setdefault("task", None)
        return CodexAppServerSession(effort=effort, **kw)

    def test_effort_in_app_server_cmd(self):
        cmd = self._make(effort="medium").build_cmd()
        assert "model_reasoning_effort=medium" in _config_values(cmd)

    def test_no_effort_no_extra_flag(self):
        cmd = self._make(effort=None).build_cmd()
        assert not any(
            value.startswith("model_reasoning_effort=")
            for value in _config_values(cmd)
        )


class TestDispatchResolveEffort:
    """Verify _accept_cc and _accept_codex resolve effort and pass it down."""

    def _make_ctx(self, provider, reasoning_effort=None, *, is_subagent=False):
        from infra.send_dispatch import DispatchContext
        from llm.resolution import ExecutionTarget
        from unittest.mock import AsyncMock, MagicMock
        harness = "claude-code" if provider == "cc" else "codex"
        selector = "sonnet" if provider == "cc" else "gpt-5.6-sol"
        expected = "claude-sonnet-4-8" if provider == "cc" else selector
        thread_row = MagicMock()
        thread_row.__getitem__ = lambda self, k: {"model": (
                                                    "claude-code/sonnet"
                                                    if provider == "cc"
                                                    else "codex/gpt-5.6-sol"
                                                ),
                                                "project_id": None}.get(k)
        from shared_send_fixtures import prepared_context_with_fit_ledger
        target = ExecutionTarget(
            catalog_id=f"{harness}/{selector}", harness=harness,
            provider=harness, backend=provider, transport_model=selector,
            expected_reported_model=expected, context_window=200000,
            available=True, availability_reason=None,
            display_name=selector, supports_reasoning=True,
        )
        return DispatchContext(
            thread_id="sess-1",
            thread_row=thread_row,
            content="hello",
            is_subagent=is_subagent,
            turn_id="turn-dispatch-effort",
            target=target,
            reasoning_effort=reasoning_effort,
            bind_input_turn=AsyncMock(),
            prepared_context=prepared_context_with_fit_ledger(target),
        )

    @pytest.mark.asyncio
    async def test_cc_default_effort_is_high(self, isolated_db, monkeypatch):
        from unittest.mock import patch
        from infra import send_dispatch as sd
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        monkeypatch.setattr(e, "_seed_effort_defaults", lambda: {"cc": "high"})
        signed_in_default("claude-code")

        captured = {}

        async def fake_feed_main(sid, content, *, model=None, effort=None, **kw):
            from core.turn_core import TurnIdentity

            captured["effort"] = effort
            return TurnIdentity("turn-cc-default-effort", 1)

        ctx = self._make_ctx("cc")
        with patch("subagents.cc_runtime.feed_main", new=fake_feed_main):
            await sd._accept_cc(ctx)

        assert captured.get("effort") == "high"

    @pytest.mark.asyncio
    async def test_cc_explicit_effort_overrides_default(self, isolated_db, monkeypatch):
        from unittest.mock import patch
        from infra import send_dispatch as sd
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        monkeypatch.setattr(e, "_seed_effort_defaults", lambda: {"cc": "high"})
        signed_in_default("claude-code")

        captured = {}

        async def fake_feed_main(sid, content, *, model=None, effort=None, **kw):
            from core.turn_core import TurnIdentity

            captured["effort"] = effort
            return TurnIdentity("turn-cc-explicit-effort", 1)

        ctx = self._make_ctx("cc", reasoning_effort="xhigh")
        with patch("subagents.cc_runtime.feed_main", new=fake_feed_main):
            await sd._accept_cc(ctx)

        assert captured.get("effort") == "xhigh"

    @pytest.mark.asyncio
    async def test_codex_max_clamped_to_xhigh_at_dispatch(self, monkeypatch):
        from unittest.mock import AsyncMock, patch
        from infra import send_dispatch as sd
        from infra import effort as e
        monkeypatch.setattr(e, "_configured_effort_defaults", lambda: {})
        monkeypatch.setattr(e, "_seed_effort_defaults", lambda: {"codex": "high"})

        captured = {}

        async def fake_feed_main(sid, content, *, effort=None, **kw):
            from core.turn_core import TurnIdentity

            captured["effort"] = effort
            return TurnIdentity("turn-codex-effort", 1)

        with patch("subagents.codex_runtime.has_thread_id", new=AsyncMock(return_value=False)), \
             patch("subagents.codex_runtime.has_prior_messages", new=AsyncMock(return_value=False)), \
             patch("subagents.codex_runtime.ensure_connection_available"), \
             patch("subagents.codex_runtime.feed_main", new=fake_feed_main):
            ctx = self._make_ctx("codex", reasoning_effort="max")
            ctx.allow_fresh = True
            await sd._accept_codex(ctx)

        assert captured.get("effort") == "xhigh"
