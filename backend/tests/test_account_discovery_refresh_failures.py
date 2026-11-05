"""Why an account refresh failed is decided by the exception's type and always logged."""
from types import SimpleNamespace

import pytest

from connections.account_discovery_refresh import refresh_account_once
from connections.harness_models import VersionTimeout


def _context(source_home='/synthetic/home'):
    return SimpleNamespace(provider='codex', source_home=source_home)


async def _refresh(context, version_read):
    recorded = []

    async def db_call(fn, *args, **kwargs):
        recorded.append(kwargs)
        return kwargs

    async def never(*args, **kwargs):
        raise AssertionError('discovery must not run after a failed check')

    await refresh_account_once(
        'codex', context=context, binary='/synthetic/codex', harness_version=None,
        adapter=(never, never, None), db_call=db_call, version_read=version_read, db=None)
    return recorded[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('error, reason', [
    (VersionTimeout('/synthetic/codex --version timed out'), 'Check timed out'),
    (RuntimeError('/synthetic/codex --version failed: timed out reading stderr'), 'Check failed'),
])
async def test_reason_follows_the_exception_type_not_its_text(error, reason, caplog):
    async def version_read(binary, env=None):
        raise error

    recorded = await _refresh(_context(), version_read)

    assert recorded['reason'] == reason
    assert 'version check failed' in caplog.text


@pytest.mark.asyncio
async def test_a_missing_account_home_is_logged_with_its_cause(caplog):
    async def version_read(binary, env=None):
        raise AssertionError('no version read without an environment')

    recorded = await _refresh(_context(source_home=None), version_read)

    assert recorded['cause'] == 'account_discovery_failed'
    assert 'reason' not in recorded
    assert 'account environment failed' in caplog.text
