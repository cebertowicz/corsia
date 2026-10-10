"""The suite's session database never carries the host's CLI sign-in state (#2975)."""

from __future__ import annotations

import pytest

from subagents.codex_runtime_config import codex_connection


@pytest.mark.parametrize("provider", ["claude-code", "github", "codex"])
def test_session_db_cli_connections_are_available_whatever_the_host(provider):
    from connections.models import get_connection

    assert (get_connection(provider) or {}).get("status") == "available"


def test_codex_connection_gate_passes_on_the_session_db():
    assert codex_connection()["provider"] == "codex"
