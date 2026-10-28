from dataclasses import replace

from core import fork_cc, fork_cc_commands
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from subagents.cc_invocation import ClaudeInvocation
from test_checkpoint_provider_adapters import _boundary, _request, _target


def test_warm_fork_uses_its_own_config_after_source_revoked(tmp_path):
    source_config = tmp_path / "source.json"
    fork_config = tmp_path / "fork.json"
    source_config.write_text("{}")
    fork_config.write_text("{}")
    invocation = ClaudeInvocation(binary="claude", permission_mode="bypassPermissions", mcp_config=str(source_config))
    source = fork_cc.CCWarmSource("thread-1", _boundary(), "source-session", "/transcript.jsonl", 12, "digest", invocation, None)
    request = replace(_request(source), target=_target("claude-code"), mcp_config=str(fork_config))
    source_config.unlink()
    command = fork_cc_commands.warm_command(source, request, CHECKPOINT_KIND, "snapshot")
    assert command[command.index("--mcp-config") + 1] == str(fork_config)
    assert fork_config.exists()
