"""Bind each admitted Codex capture to the launch and tool-filter surface."""
import hashlib
import inspect
import json

from subagents import codex_confinement as confinement
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
            dynamic_tool_definitions, project_definitions,
        )],
    }
    digest = hashlib.sha256(json.dumps(surface, sort_keys=True).encode()).hexdigest()
    assert all(recorded == digest for recorded in confinement.CAPTURED_CODEX_VERSIONS.values()), digest
