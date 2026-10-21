"""Actual post-execution pipe failures must retain browser ownership."""
import asyncio
from asyncio import unix_events
import sys

import pytest
from host_access import browser_cli as cli, browser_sessions as sessions
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


class PipeFailure:
    """Inject only pipe setup failure after a real inert client has executed."""

    def __init__(self, tmp_path):
        self.marker = tmp_path / 'executed'
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()
        self.release = asyncio.Event()
        self.transports = []
        self.error = RuntimeError('private-argv private-env private-session /private/path')

    async def connect_pipes(self, waiter):
        """Fail the transport's pipe-connect stage, as asyncio reports it to the spawn.

        The client's output goes to files, so asyncio connects no read pipe for it; this stage still
        runs after the client has executed and is where a pipe setup failure reaches the spawn.
        """
        try:
            await self.fail_pipe()
        except BaseException as exc:
            if not waiter.cancelled():
                waiter.set_exception(exc)

    async def fail_pipe(self):
        self.entered.set()
        await self.release.wait()
        # The real child watcher has reported exit (settle waits for it), just as the
        # independent reproducer does. Otherwise transport shutdown can mask the tested error.
        assert self.transports[0]._returncode == 0
        assert self.marker.read_text() == 'executed'
        raise self.error

    async def invoke(self, argv, *, timeout):
        return await cli.invoke(
            (sys.executable, '-c',
             "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('executed')",
             str(self.marker)), socket_dir=self.marker.parent, timeout=timeout,
        )

    async def settle(self, coroutine, cancel):
        task = asyncio.create_task(coroutine)
        await asyncio.wait_for(self.entered.wait(), 3)
        # Client runtime under load must not spend invoke's pending-spawn cancellation wait.
        await asyncio.wait_for(self.exited.wait(), 30)
        if cancel:
            task.cancel()
        self.release.set()
        return await asyncio.wait_for(task, 7)


@pytest.fixture
async def pipe_failure(monkeypatch, tmp_path):
    probe = PipeFailure(tmp_path)
    original_start = unix_events._UnixSubprocessTransport._start
    original_exited = unix_events._UnixSubprocessTransport._process_exited

    def start(transport, *args, **kwargs):
        original_start(transport, *args, **kwargs)
        probe.transports.append(transport)

    def process_exited(transport, returncode):
        original_exited(transport, returncode)
        probe.exited.set()

    monkeypatch.setattr(unix_events._UnixSubprocessTransport, '_start', start)
    monkeypatch.setattr(unix_events._UnixSubprocessTransport, '_process_exited', process_exited)
    monkeypatch.setattr(unix_events._UnixSubprocessTransport, '_connect_pipes', probe.connect_pipes)
    yield probe
    probe.release.set()
    for transport in probe.transports:
        if transport._proc.poll() is None:
            transport._proc.kill()
        await asyncio.to_thread(transport._proc.wait, timeout=EXIT_TIMEOUT)


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('error_type,cause', [(RuntimeError, 'runtime_error'), (FileNotFoundError, 'not_found'), (PermissionError, 'permission_error')])
async def test_executed_client_pipe_failure_is_unknown(pipe_failure, cancel, caplog, error_type, cause):
    pipe_failure.error = error_type('private-argv private-env private-session /private/path')
    attempt = pipe_failure.invoke((), timeout=30)
    if cancel:
        with pytest.raises(cli.CommandCancelled) as caught:
            await pipe_failure.settle(attempt, cancel)
        result = caught.value.result
    else:
        result = await pipe_failure.settle(attempt, cancel)
    assert pipe_failure.marker.read_text() == 'executed'
    assert pipe_failure.transports[0]._proc.returncode == 0
    assert result.completion == 'unknown'
    phase = 'late_spawn' if cancel else 'spawn'
    assert f'browser client failure: phase={phase}; cause={cause}' in caplog.text
    assert 'private-' not in caplog.text + repr(result)
    assert '/private/path' not in caplog.text + repr(result)
    assert str(pipe_failure.marker) not in caplog.text + repr(result)
    rendered = cli.render_model_result(result, redactions={})
    assert rendered.startswith('Error: agent-browser command failed') and 'private' not in rendered


@pytest.mark.parametrize('route', ['thread', 'lease', 'shutdown'])
@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('cancel', [False, True])
async def test_pipe_failure_retains_owner_across_cleanup(
    synthetic_browser, pipe_failure, monkeypatch, route, bound, cancel, caplog,
):
    fake = synthetic_browser
    owner = fake.owner
    await owner._pin_native()
    if bound:
        await owner.run('thread', ['snapshot'], lease_id='lease')
    monkeypatch.setattr(owner, '_invoke', pipe_failure.invoke)
    attempt = owner.run('thread', ['snapshot'], lease_id='lease', timeout=30)
    expected = cli.CommandCancelled if cancel else sessions.BrowserSessionCleanupError
    with pytest.raises(expected):
        await pipe_failure.settle(attempt, cancel)
    runtime = owner._sessions['thread']
    assert runtime.attempt.completion == 'unknown' and owner.live_count() == 1
    assert runtime.record.terminal_state == 'death_unverified'
    assert (runtime.record.pid is not None) == bound
    assert 'cause=runtime_error' in caplog.text
    for _ in range(2):
        with pytest.raises(sessions.BrowserSessionCleanupError):
            if route == 'thread':
                await owner.close_thread('thread')
            elif route == 'lease':
                await owner.terminate_lease('lease')
            else:
                await owner.shutdown()
        assert owner._sessions['thread'] is runtime
        assert runtime.attempt.completion == 'unknown' and owner.live_count() == 1
        if bound:
            assert fake.children[runtime.record.session].poll() is None
    assert 'private-' not in caplog.text
    assert runtime.record.session not in caplog.text
