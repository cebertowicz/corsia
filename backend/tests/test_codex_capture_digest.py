"""Detect changes to the launch and tool surface since the listed captures.

On a change, run the capture test for every listed version before updating the digest.
"""
import hashlib
import inspect
import json

from subagents import codex_confinement as confinement
from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_app_server_events import to_dynamic_tool_specs
from subagents.codex_launch_policy import build_cmd
from subagents.harness_tool_bridge import dynamic_tool_definitions
from subagents.managed_launch import (
    _CODEX_APP_SERVER_CONTROL_KEYS, _codex_config_args,
    codex_app_server_config_override_args, codex_app_server_governance_args,
)
from tools.confined_reads import project_definitions


def test_captured_versions_match_launch_surface():
    surface = {
        "config": confinement.CONFIG,
        "governance_args": codex_app_server_governance_args(),
        "governed_keys": sorted(_CODEX_APP_SERVER_CONTROL_KEYS),
        "read_tools": sorted(confinement.UNTRUSTED_READ_TOOLS),
        "implementation": [inspect.getsource(function) for function in (
            build_cmd, _codex_config_args, codex_app_server_config_override_args,
            codex_app_server_governance_args,
            confinement.config_args, confinement.prepare_launch, confinement.assert_tools,
            dynamic_tool_definitions, project_definitions, to_dynamic_tool_specs,
            CodexAppServerSession._start_or_resume_thread, CodexAppServerSession.start_turn,
        )],
    }
    digest = hashlib.sha256(json.dumps(surface, sort_keys=True).encode()).hexdigest()
    unchanged = digest == confinement.CAPTURED_SURFACE_DIGEST
    assert unchanged, (
        "Launch or tool surface changed since capture; run "
        "backend/tests/test_codex_confined_capture.py on every listed Codex version "
        "before updating CAPTURED_SURFACE_DIGEST."
    )
