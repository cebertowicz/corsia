"""Strict producer evidence, version/mode policy and tool-facing output."""
import asyncio
import json
import threading
import time

import pytest
from host_access import browser_cli as cli, browser_identity as identity
from tests.browser_process_fixture import EXIT_TIMEOUT, synthetic_browser  # noqa: F401


@pytest.mark.parametrize('error_type', sorted(cli._PRE_DAEMON_ERRORS))
async def test_typed_pre_daemon_errors_from_real_subprocess(tmp_path, error_type):
    import sys
    payload = json.dumps({'success': False, 'error': 'invalid command', 'errorType': error_type})
    result = await cli.invoke((sys.executable, '-c', 'import sys; print(sys.argv[1]);sys.exit(1)', payload),
                              socket_dir=tmp_path, timeout=EXIT_TIMEOUT)
    assert result.rejected_before_daemon()


@pytest.mark.parametrize('payload', [
    {}, {'success': False, 'error': 'Daemon failed to start'},
    {'success': False, 'error': 'bad', 'errorType': []},
    {'success': False, 'error': 'bad', 'errorType': 'new_error'},
    {'success': True, 'error': 'bad', 'errorType': 'unknown_command'},
    {'success': 0, 'error': 'bad', 'errorType': 'unknown_command'},
])
def test_untyped_or_invalid_output_never_proves_no_effect(payload):
    assert not cli.CommandResult(1, json.dumps(payload), '').rejected_before_daemon()


@pytest.mark.parametrize('raw', ['{', 'prefix {}', '{} {}', '[]', 'null'])
def test_partial_or_multiple_json_values_are_not_evidence(raw):
    assert not cli.CommandResult(1, raw, '').rejected_before_daemon()


@pytest.mark.parametrize('output,family', [('agent-browser 0.27.0', '0.27.'),
                                           ('agent-browser  0.27.9\n', '0.27.'),
                                           ('agent-browser 0.31.2', '0.31.')])
def test_patch_family_version_admission(output, family, monkeypatch):
    monkeypatch.delenv('CORSIA_AGENT_BROWSER_VERSION_RANGE', raising=False)
    assert cli.admitted_version(output).startswith(family)


@pytest.mark.parametrize('output', ['agent-browser 0.26.9', 'agent-browser 0.28.0',
                                     'agent-browser 0.30.4', 'agent-browser 0.32.0',
                                     'agent-browser 0.27.0-dev', 'Python 3.11.16'])
def test_unverified_version_families_rejected(output, monkeypatch):
    monkeypatch.delenv('CORSIA_AGENT_BROWSER_VERSION_RANGE', raising=False)
    with pytest.raises(identity.BrowserIdentityError):
        cli.admitted_version(output)


def test_operator_version_policy_is_explicit(monkeypatch):
    monkeypatch.setenv('CORSIA_AGENT_BROWSER_VERSION_RANGE', '==0.27.2')
    assert cli.admitted_version('agent-browser 0.27.2') == '0.27.2'
    with pytest.raises(identity.BrowserIdentityError):
        cli.admitted_version('agent-browser 0.27.0')
    monkeypatch.setenv('CORSIA_AGENT_BROWSER_VERSION_RANGE', 'invalid-policy')
    with pytest.raises(identity.BrowserIdentityError, match='policy is invalid'):
        cli.admitted_version('agent-browser 0.27.0')


@pytest.mark.parametrize('data,expected', [
    ({'snapshot': '- button "Submit" [ref=e1]', 'refs': {'e1': {}}}, '- button "Submit" [ref=e1]'),
    ({'url': 'about:blank', 'title': 'Blank'}, 'Blank\nabout:blank'),
    ({'result': {'ok': True}}, '{"ok": true}'),
    ({'result': 'done'}, 'done'),
    ({'path': '/output/screenshot.png'}, 'Screenshot saved to /output/screenshot.png'),
    ({'lifecycle': {'reused': True}, 'url': 'about:blank', 'title': 'Blank'}, 'Blank\nabout:blank'),
    ({'lifecycle': {'reused': True}, 'title': 'Blank'}, '{"title": "Blank"}'),
    ({'lifecycle': {'reused': True}}, '(no output)'),
])
def test_native_json_preserves_readable_tool_output(data, expected):
    result = cli.CommandResult(0, json.dumps({'success': True, 'data': data}), '')
    assert cli.render_model_result(result, redactions={}) == expected


@pytest.mark.parametrize('config', [{'provider': 'browserbase'}, {'provider': 'ios'},
                                    {'engine': 'lightpanda'}, {'cdp': '9222'}, {'autoConnect': True}])
async def test_unsupported_config_mode_rejected_before_any_session_effect(synthetic_browser, monkeypatch, tmp_path, config):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('AGENT_BROWSER_CONFIG', str(path))
    with pytest.raises(Exception, match='verified local Chrome/CDP'):
        await synthetic_browser.owner.run('thread', ['snapshot'])
    assert synthetic_browser.owner.live_count() == 0 and not synthetic_browser.calls


async def test_all_identity_introspection_runs_off_event_loop(synthetic_browser, monkeypatch):
    loop_thread = threading.get_ident()
    original = identity.observe_session
    observed_threads = []

    def observe(**kwargs):
        observed_threads.append(threading.get_ident())
        time.sleep(0.01)
        return original(**kwargs)

    monkeypatch.setattr(identity, 'observe_session', observe)
    await synthetic_browser.owner.run('thread', ['snapshot'])
    await synthetic_browser.owner.close_thread('thread')
    assert observed_threads and all(thread != loop_thread for thread in observed_threads)


class _RecordingWriter:
    def __init__(self):
        self.sent = b''

    def write(self, data):
        self.sent += data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


@pytest.mark.parametrize('data,accepted', [
    ({'closed': True}, True),
    # 0.31 adds lifecycle and persistence status beside the acknowledgement.
    ({'closed': True, 'lifecycle': {'reused': False}, 'restoreStatus': 'not_configured',
      'saveStatus': 'not_configured'}, True),
    ({'closed': False}, False), ({'closed': 'true'}, False), ({}, False), (None, False),
])
async def test_close_acknowledgement_requires_closed_true(data, accepted):
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps({'id': 'req', 'success': True, 'data': data}) + '\n').encode())
    reader.feed_eof()
    writer = _RecordingWriter()
    if accepted:
        await cli.close_request(reader, writer, 'req')
    else:
        with pytest.raises(identity.BrowserIdentityError):
            await cli.close_request(reader, writer, 'req')
    assert json.loads(writer.sent) == {'id': 'req', 'action': 'close'}
