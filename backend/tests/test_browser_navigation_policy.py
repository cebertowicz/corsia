"""Native navigation uses the shared pane URL policy before session effects."""
from unittest.mock import AsyncMock

import pytest

from host_access import browser_navigation, browser_sessions
from host_access.browser_session_state import BrowserSessionError


NAVIGATIONS = [
    ('open', '{url}'), ('goto', '{url}'), ('navigate', '{url}'),
    ('read', '{url}'), ('read', '--raw', '--filter', 'content', '{url}'),
    ('tab', 'new', '{url}'), ('tab', 'new', '--label', 'fixture', '{url}'),
    ('diff', 'url', 'https://example.com', '{url}'),
    ('diff', 'url', '{url}', 'https://example.com'),
    ('record', 'start', 'fixture.webm', '{url}'),
    ('record', 'restart', 'fixture.webm', '{url}'),
    ('vitals', '{url}'), ('web-vitals', '--json', '{url}'), ('pushstate', '{url}'),
    ('batch', 'open {url}'), ('batch', 'tab new --label fixture {url}'),
]


@pytest.mark.parametrize('shape', NAVIGATIONS)
@pytest.mark.parametrize('url', ['file:///synthetic/private.json', 'view-source:https://example.com',
                                 'filesystem:https://example.com/private', 'chrome://version',
                                 'devtools://devtools/', 'javascript:void(0)'])
async def test_disallowed_url_never_reaches_native_or_admission(shape, url, monkeypatch):
    owner = browser_sessions.BrowserSessionOwner(namespace='corsia-navigation-policy')
    pin = AsyncMock(side_effect=AssertionError('native pin/probe preceded URL refusal'))
    monkeypatch.setattr(owner, '_pin_native', pin)
    args = tuple(value.replace('{url}', url) for value in shape)
    with pytest.raises(BrowserSessionError, match='HTTP, HTTPS or about:blank'):
        await owner.run('fixture', args)
    pin.assert_not_called()
    assert owner.live_count() == 0


@pytest.mark.parametrize('shape', NAVIGATIONS)
def test_every_navigation_form_reuses_address_normalization(shape):
    args = tuple(value.replace('{url}', 'example.com') for value in shape)
    normalized = browser_navigation.normalize_command(args)
    assert 'https://example.com' in ' '.join(normalized)
    assert 'file:' not in ' '.join(normalized)


def test_url_less_commands_and_text_are_preserved():
    for command in [('tab', 'new'), ('read', '--filter', 'file:///literal'), ('record', 'stop'),
                    ('get', 'text', 'body'), ('fill', '#input', 'file:///literal')]:
        assert browser_navigation.normalize_command(command) == command


@pytest.mark.parametrize('command', [('open', 'about:blank'), ('tab', 'new', 'about:blank')])
def test_blank_page_is_available(command):
    assert browser_navigation.normalize_command(command) == command


async def test_trusted_tool_navigation_is_checked_before_native_pin(monkeypatch):
    owner = browser_sessions.BrowserSessionOwner(namespace='corsia-navigation-policy')
    pin = AsyncMock(side_effect=AssertionError('native pin preceded URL refusal'))
    monkeypatch.setattr(owner, '_pin_native', pin)
    url = 'file:///synthetic/private.json'
    import json
    with pytest.raises(BrowserSessionError, match='HTTP, HTTPS or about:blank'):
        await owner.run('fixture', ('eval', f"location.href = {json.dumps(url)}; 'navigating'"), navigation_url=url)
    pin.assert_not_called()
