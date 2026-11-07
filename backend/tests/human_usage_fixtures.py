"""Disposable held threads and synthetic vendor I/O for human-send integration proof."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from vendor_account_fixtures import accounts, notification_owner, set_legacy_policy, signed_in_default  # noqa: F401


class CaptureSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class VendorIO:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.reads = []
        self.verdict = 'limited'
        self.sessions = []

    async def report(self, provider, receipt=None):
        self.reads.append((provider, receipt))
        self.started.set()
        await self.finish.wait()
        if self.verdict == 'error':
            raise RuntimeError('synthetic control failure')
        if self.verdict == 'unknown':
            return None
        if provider == 'codex':
            return {'ordinaryUsageAllowed': self.verdict == 'allowed', 'rateLimits': {}}
        return {'rate_limits_available': True, 'rate_limits': {
            'five_hour': {'utilization': 0 if self.verdict == 'allowed' else 100,
                          'resets_at': '2099-01-01T00:00:00Z'},
        }}

    def codex_session(self, **kwargs):
        session = CodexSession(self, **kwargs)
        self.sessions.append(session)
        return session

    async def spawn(self, *args, **kwargs):
        assert '--safe-mode' in args and '--no-session-persistence' in args
        return ClaudeProcess(self)

    async def version(self, *args, **kwargs):
        return 'synthetic-version'

    async def debug(self, *args, **kwargs):
        return {'models': [{'slug': 'test', 'context_window': 262144}]}


class CodexSession:
    def __init__(self, control, **kwargs):
        assert kwargs['control_only'] is True
        self.control = control
        self.home = Path(kwargs['codex_home'])
        self.closed = False

    async def start(self):
        assert (self.home / 'auth.json').is_symlink()
        (self.home / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'test'}]}))

    async def list_models(self):
        return {'data': [{'id': 'test', 'model': 'synthetic-test', 'displayName': 'Test'}]}

    async def _request(self, method, params):
        assert method == 'account/rateLimits/read' and params is None
        return await self.control.report('codex', self.vendor_account_receipt)

    def build_env(self):
        return {'CODEX_HOME': str(self.home)}

    async def close(self, **kwargs):
        self.closed = True


class ClaudeProcess:
    returncode = 0

    def __init__(self, control):
        self.control = control
        self._transport = None

    async def communicate(self, payload):
        frames = [json.loads(line) for line in payload.splitlines()]
        assert [frame['request']['subtype'] for frame in frames] == ['get_usage', 'end_session']
        report = await self.control.report('claude-code')
        return json.dumps({'response': {'subtype': 'success', 'request_id': 'corsia-usage',
                                       'response': report}}).encode(), b''

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture(params=['codex', 'claude-code'])
def held_subject(accounts, request, monkeypatch):
    return make_held_subject(accounts, request.param, monkeypatch)


@pytest.fixture(params=['codex', 'claude-code'])
def unbound_held_subject(accounts, request, monkeypatch):
    return make_held_subject(accounts, request.param, monkeypatch, bound=False)


def make_held_subject(accounts, provider, monkeypatch, *, bound=True):
    """Create the same held FIFO with or without a prior thread account choice."""
    from connections import harness_usage, harness_usage_check as checks
    from connections.account_policy import select_account
    from connections.account_receipts import capture_receipt
    from connections.models import upsert_connection
    from infra.input_admission import accept_thread_input

    db, account, *_ = accounts
    model = provider + '/test'
    if provider == 'claude-code':
        upsert_connection(type='cli', provider=provider, status='available', config={'binary': '/bin/test'})
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
                   "VALUES (?,'Test',?,?,'test',?,'t','t')", (model, model, provider, provider))
        db.execute("UPDATE threads SET model=? WHERE id='destination'", (model,))
        db.commit()
        account = signed_in_default(provider, db=db)
    db.execute('UPDATE models SET context_window=262144 WHERE id=?', (model,))
    db.commit()
    set_legacy_policy('destination-project', provider, allowlist=[account['id']],
                      default_account_id=account['id'], switch_on_limit=False, db=db)
    if bound:
        select_account('destination', provider, account_id=account['id'], db=db)
    receipt = capture_receipt(provider, account_id=account['id'], db=db)
    harness_usage.record_limited(provider=provider, receipt=receipt, reason='usage_limit',
                                 resets_at='2099-01-01T00:00:00+00:00', source='test', db=db)
    accept_thread_input('destination', role='user', content='older held input', intent='apply_now',
                        message_id='older', created_at='2026-11-06T19:00:00Z', db=db)
    db.commit()
    monkeypatch.setattr(checks, '_CHECKS', {})
    monkeypatch.setattr(checks, '_LAST_STARTED', {account['id']: time.monotonic()})
    monkeypatch.setattr(checks, '_NEW_LOGIN_PENDING', set())
    return db, provider, model, account, receipt


@pytest_asyncio.fixture
async def vendor_io(monkeypatch):
    from connections import codex_discovery, harness_models
    # Startup imports ws before accepting input; keep that outside action timeouts.
    import ws  # noqa: F401

    control = VendorIO()
    monkeypatch.setattr(codex_discovery, 'CodexAppServerSession', control.codex_session)
    monkeypatch.setattr(codex_discovery, 'discover_codex_debug_models', control.debug)
    monkeypatch.setattr(harness_models, 'binary_version', control.version)

    monkeypatch.setattr('connections.claude_discovery.asyncio.create_subprocess_exec', control.spawn)
    yield control
    control.finish.set()
    await drain_background()


async def drain_background():
    from ws.ingress import background_tasks

    await asyncio.sleep(0)
    while pending := {task for task in background_tasks if task.get_loop() is asyncio.get_running_loop()
                      and not task.done()}:
        await asyncio.wait_for(asyncio.gather(*pending), 5)


async def human_send(socket, *, worker=False, input_id='fresh', user='user', **data):
    import ws

    if worker:
        await ws._handle_subagent_feed(socket, {'id': user}, 'destination', 'try now',
                                      input_id=input_id, **data)
    else:
        await ws._handle_message(socket, {'id': user}, user, {
            'thread_id': 'destination', 'input_id': input_id, 'content': 'try now', **data,
        })
