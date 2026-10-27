import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools import linux_sandbox_probe as probe


MISSING = ('Error: the Linux sandbox (bubblewrap) is not installed on this computer. '
           'Nothing ran. Install it with "sudo apt install bubblewrap", then restart CORSIA.')


def failure(detail):
    return ('Error: the Linux sandbox (bubblewrap) is installed but could not complete its startup check. '
            'Nothing ran. On Ubuntu 24.04 or later, rerun the CORSIA installer to add its '
            'AppArmor rule, then restart CORSIA.')


@pytest.fixture(autouse=True)
def clear_probe():
    probe.invalidate_sandbox_probe()
    yield
    probe.invalidate_sandbox_probe()


@pytest.fixture
def executable(tmp_path, monkeypatch):
    path = tmp_path / 'bwrap'
    path.write_text('binary')
    monkeypatch.setattr(probe.shutil, 'which', lambda name: str(path))
    return path


def result(stdout='', stderr='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(probe.shutil, 'which', lambda _: None)
    assert probe.sandbox_error() == MISSING


@pytest.mark.parametrize(('output', 'detail'), [
    ('bubblewrap 0.7.9', 'bubblewrap 0.8.0 or newer is required (found 0.7.9)'),
    ('unknown', 'could not determine bubblewrap version'),
])
def test_bad_version(executable, monkeypatch, output, detail):
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw: result(stdout=output))
    error = probe.sandbox_error()
    if output == 'bubblewrap 0.7.9':
        assert 'bubblewrap 0.8.0 or newer is required (found 0.7.9)' in error
        assert 'Upgrade bubblewrap' in error
    else:
        assert error == failure(detail)


def test_probe_failure_logs_detail_and_retries(executable, monkeypatch, caplog):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return result(stdout='bubblewrap 0.9.0') if args[-1] == '--version' else result(stderr='denied\nextra', returncode=1)
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.sandbox_error() == failure('denied')
    assert 'denied\nextra' in caplog.text
    assert 'denied' not in probe.sandbox_error()
    assert len(calls) == 4
    assert calls[1][0] == probe.launcher_argv([str(executable), *probe.NAMESPACES, '--ro-bind', '/', '/', '--proc', '/proc', '--', '/bin/true'])
    assert calls[1][1] == {'capture_output': True, 'text': True, 'timeout': 5}


def test_exception_is_fail_closed(executable, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('bwrap', 5)
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.sandbox_error() == failure("Command 'bwrap' timed out after 5 seconds")


def test_success_cache_and_identity_changes(executable, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return result(stdout='bubblewrap 0.8.0') if args[-1] == '--version' else result()
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.sandbox_error() is None
    assert probe.sandbox_error() is None
    assert len(calls) == 2
    executable.write_text('replacement binary')
    assert probe.sandbox_error() is None
    assert len(calls) == 4
    executable.unlink()
    assert probe.sandbox_error() is not None


def test_concurrent_checks_share_one_successful_probe(executable, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return result(stdout='bubblewrap 0.9.0') if args[-1] == '--version' else result()
    monkeypatch.setattr(probe.subprocess, 'run', run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: probe.sandbox_error(), range(8))) == [None] * 8
    assert len(calls) == 2


def test_invalidate_reprobes_and_failure_can_recover(executable, monkeypatch):
    outcomes = iter([result(), result(stderr='unavailable', returncode=1), result()])
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return result(stdout='bubblewrap 0.9.0') if args[-1] == '--version' else next(outcomes)
    monkeypatch.setattr(probe.subprocess, 'run', run)
    assert probe.sandbox_error() is None
    probe.invalidate_sandbox_probe()
    assert probe.sandbox_error() == failure('unavailable')
    assert probe.sandbox_error() is None
    assert len(calls) == 6


@pytest.mark.parametrize(('code', 'stderr', 'timeout', 'cleared'), [
    (1, b'bwrap: setup failed\n', False, True),
    (2, b'ordinary error\n', False, False),
    (1, b'bwrap: setup failed\n', True, False),
])
def test_possible_setup_error_requests_reprobe(monkeypatch, code, stderr, timeout, cleared):
    from tools import shell_sandbox
    from types import SimpleNamespace
    monkeypatch.setattr(shell_sandbox, 'sys', SimpleNamespace(platform='linux'))
    calls = []
    monkeypatch.setattr(probe, 'invalidate_sandbox_probe', lambda: calls.append(True))
    shell_sandbox.recheck_after_launch_error(code, stderr, timed_out=timeout)
    assert bool(calls) == cleared


def test_child_spoofed_bwrap_message_causes_harmless_reprobe(monkeypatch):
    from tools import shell_sandbox
    from types import SimpleNamespace
    monkeypatch.setattr(shell_sandbox, 'sys', SimpleNamespace(platform='linux'))
    calls = []
    monkeypatch.setattr(probe, 'invalidate_sandbox_probe', lambda: calls.append(True))
    shell_sandbox.recheck_after_launch_error(1, b'bwrap: x\n')
    assert calls == [True]


@pytest.mark.asyncio
async def test_script_uses_shared_startup_gate(monkeypatch):
    from automation import script_sandbox
    calls = []

    async def gate(available, version_error, **kwargs):
        calls.append((available, version_error, kwargs))
        return 'refused'

    monkeypatch.setattr(script_sandbox.shell_sandbox, 'startup_error', gate)
    with pytest.raises(script_sandbox.ScriptSandboxError, match='refused'):
        await script_sandbox._checked_environment()
    assert calls == [(script_sandbox.is_safehouse_available,
                      script_sandbox.safehouse_version_error,
                      {'prefix': 'sandbox unavailable: '})]


@pytest.mark.asyncio
async def test_background_command_failure_keeps_cached_probe(executable, monkeypatch):
    from types import SimpleNamespace
    from automation import managed_process_events
    from infra import db_async
    import tools.shell_sandbox as shell_sandbox
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return result(stdout='bubblewrap 0.9.0') if args[-1] == '--version' else result()
    async def no_persistence(*args, **kwargs):
        return None
    monkeypatch.setattr(probe.subprocess, 'run', run)
    monkeypatch.setattr(shell_sandbox, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(managed_process_events, 'row_for', lambda record: {})
    monkeypatch.setattr(db_async, 'run_in_db', no_persistence)
    assert probe.sandbox_error() is None
    assert len(calls) == 2
    await managed_process_events.on_background_transition(SimpleNamespace(), 'failed')
    assert probe.sandbox_error() is None
    assert len(calls) == 2
