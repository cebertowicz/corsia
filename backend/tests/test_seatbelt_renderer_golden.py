"""Byte-for-byte regression pins for the existing Seatbelt renderers."""

import json
from pathlib import Path

from pytest import MonkeyPatch

from tools import host_path_policy, shell_guard
from tools.host_path_policy import ProtectedHostPathPolicy
from infra.project_repo_policy import RepoPolicy


FIXTURE = Path(__file__).parent / "fixtures" / "seatbelt_renderer_golden.json"


def render_baseline(monkeypatch: MonkeyPatch) -> dict[str, str]:
    from host_access import browser_ports
    monkeypatch.setattr(browser_ports, 'SOCKET_ROOT', Path('/golden/browser-sockets'))
    monkeypatch.setattr(browser_ports, 'RESERVED_PORTS', (39400, 39401))
    home = Path('/golden/home')
    repo = Path('/golden/work/repo')
    profile = Path('/golden/state/profile.sb')
    # /tmp resolves to /private/tmp on macOS but not on Linux.
    monkeypatch.setattr(host_path_policy, '_CONTROL_FILES', tuple(
        Path('/private/tmp') / name
        for name in ('corsia-restart', 'corsia-rebuild-ui', 'corsia-restart.log')
    ))
    monkeypatch.setattr(shell_guard.shutil, 'which', lambda _: '/golden/bin/safehouse')
    monkeypatch.setattr(shell_guard, '_safehouse_executable', lambda: Path('/golden/bin/safehouse'))
    # Prevent platform-specific symlink resolution of system executables.
    monkeypatch.setattr(shell_guard, '_canonical_paths', lambda paths: list(dict.fromkeys(paths)))
    monkeypatch.setattr(shell_guard, '_sandbox_path', lambda path: shell_guard.sandbox_literal(path))
    monkeypatch.setattr(shell_guard, '_verified_worktrees', lambda *_: [])
    monkeypatch.setattr(shell_guard, 'list_unsupported_pinned_repos', lambda _: [])
    monkeypatch.setattr(shell_guard, 'unix_socket_sandbox_rules', lambda _: '(deny network-bind (literal "/golden/socket"))')
    monkeypatch.setattr(shell_guard, 'agent_account_deny_rule', lambda *_: None)
    monkeypatch.setattr(
        shell_guard, 'current_protected_host_path_policy',
        lambda **kwargs: ProtectedHostPathPolicy(home, Path('/golden/state'), **kwargs),
    )
    policy = RepoPolicy('project', 'repo', repo, 'main', True)
    path_policy = ProtectedHostPathPolicy(
        home, Path('/golden/state'), generated_profile=profile,
        safehouse_paths=(Path('/golden/bin/safehouse'),),
        write_roots=(Path('/golden/work/protected'),),
    )
    return {
        'path_policy': path_policy.sandbox_rules(),
        'protected_repo': shell_guard.execution_envelope_profile(
            'pwd', repo, profile, leased=False, policies=[policy], thread_id='thread-1',
        ),
        'leased': shell_guard.execution_envelope_profile(
            'pwd', repo, profile, leased=True, thread_id='thread-1',
        ),
    }


def test_seatbelt_renderers_match_baseline(monkeypatch):
    expected = json.loads(FIXTURE.read_text(encoding='utf-8'))
    actual = render_baseline(monkeypatch)
    assert actual.keys() == expected.keys()
    for name in expected:
        assert actual[name].encode('utf-8') == expected[name].encode('utf-8'), name

