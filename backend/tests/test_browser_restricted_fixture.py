"""Event ordering for the synthetic restricted-port fixture's title reader."""
import json

import pytest

from tests.browser_restricted_ports_fixture import _title


class Page:
    def __init__(self, events, *, blank=False):
        self.events = iter(events)
        self.frame = {'id': 'main', 'loaderId': 'initial' if blank else 'final',
                      'url': 'about:blank' if blank else 'http://fixture/page'}
        self.socket = self
        self.sent = []
        self.evaluated = False
        self.ready = False
        self.commands = []

    async def recv(self):
        event = next(self.events)
        if event.pop('ready', False):
            self.ready = True
        return json.dumps(event)

    async def send(self, method, **params):
        self.sent.append(method)
        return len(self.sent)

    async def call(self, method, **params):
        self.commands.append(method)
        if method == 'Page.getFrameTree':
            return {'frameTree': {'frame': self.frame}}
        if method == 'Runtime.evaluate':
            # A target and command responses can precede its default context.
            assert self.ready, 'Cannot find default execution context'
            assert params['contextId'] == 9
            self.evaluated = True
            return {'result': {'value': {'title': 'fixture title', 'ready': 'complete',
                                         'url': 'http://fixture/page'}}}
        return {}


def event(method, **params):
    return {'method': method, 'params': params}


def context(identifier=9, *, frame='main', default=True):
    return event('Runtime.executionContextCreated', context={
        'id': identifier, 'auxData': {'isDefault': default, 'frameId': frame}})


def loaded(loader='final'):
    return event('Page.lifecycleEvent', frameId='main', loaderId=loader, name='load')


@pytest.mark.parametrize('load_first', [False, True])
@pytest.mark.parametrize('blank', [False, True])
async def test_title_waits_for_loaded_top_frame_default_context(load_first, blank):
    events = [{'id': 1, 'result': {}}, {'id': 2, 'result': {}},
              context(2, frame='child'), context(3, default=False)]
    if blank:
        events += [context(4), loaded('initial'),
                   event('Page.frameNavigated', frame={
                       'id': 'main', 'loaderId': 'final', 'url': 'http://fixture/page'}),
                   event('Runtime.executionContextsCleared')]
    readiness = [loaded(), context()] if load_first else [context(), loaded()]
    readiness[-1]['ready'] = True
    page = Page(events + readiness, blank=blank)
    assert await _title(page) == 'fixture title'
    assert page.evaluated
    assert page.commands[-2:] == ['Runtime.disable', 'Page.setLifecycleEventsEnabled']


async def test_destroyed_context_is_not_evaluated():
    final = context()
    load = loaded()
    load['ready'] = True
    page = Page([context(4), loaded(),
                 event('Runtime.executionContextDestroyed', executionContextId=4),
                 {'id': 1, 'result': {}}, {'id': 2, 'result': {}}, final, load])
    assert await _title(page) == 'fixture title'


async def test_error_document_load_does_not_require_frame_navigated():
    final = loaded('error-document')
    final['ready'] = True
    page = Page([{'id': 1, 'result': {}}, {'id': 2, 'result': {}}, context(4),
                 event('Runtime.executionContextsCleared'), context(), final], blank=True)
    assert await _title(page) == 'fixture title'


async def test_enable_errors_are_not_hidden():
    page = Page([{'id': 1, 'error': {'message': 'enable failed'}}])
    with pytest.raises(AssertionError, match='enable failed'):
        await _title(page)
    assert not page.evaluated
    assert page.commands[-2:] == ['Runtime.disable', 'Page.setLifecycleEventsEnabled']
