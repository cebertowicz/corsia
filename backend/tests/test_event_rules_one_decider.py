"""The rule evaluator alone decides a webhook's recipient or drop."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from events.platform import deliver_platform_event_sync
from infra.database import write_transaction
from tests.event_routing_helpers import (
    check_suite_payload, mk_session, pr_payload, register_pr, route,
)


def _every_rule(db, rule_id, *, to, event_type='webhook.github.pr_merged'):
    db.execute('''INSERT INTO event_rules
        (id,created_at,updated_at,scope,connector,event_type,"match","on",action,"to",mode,precedence)
        VALUES (?,'a','b','global','github',?,'[]','every','deliver',?,'wake',0)''',
        (rule_id, event_type, to))


def _merged():
    payload = pr_payload('owner/repo', 7, action='closed')
    payload['pull_request']['merged'] = True
    return payload


def _route_events(emit):
    return [(c.args[1], c.kwargs['properties']) for c in emit.call_args_list
            if c.args[0] == 'event_route']


@pytest.mark.asyncio
async def test_seeded_drop_row_decides_check_event_before_pr_lookup(isolated_db):
    from events import router

    register_pr('o/r', 71, mk_session('wk-cancel'))
    with (patch.object(router, 'check_suite_pr_metadata_from_event') as lookup,
          patch('events.platform.emit_product_event') as emit,
          patch('events.router.delivery.deliver_to_thread') as deliver):
        res = await route('webhook.github.check_suite_completed',
                          check_suite_payload('o/r', 71, conclusion='cancelled'))
    lookup.assert_not_called()
    deliver.assert_not_called()
    assert res.outcome == router.RouteOutcome.PARKED
    assert _route_events(emit) == [('suppressed', {
        'internal_event': 'webhook.github.check_suite_completed',
        'author': 'alice', 'rule_id': 'rule_set:github@1:cancelled'})]


@pytest.mark.asyncio
async def test_self_authored_drop_logs_product_event_with_author_and_rule(isolated_db):
    from events.router import RouteOutcome

    register_pr('owner/repo', 90, mk_session('owner'))
    payload = pr_payload('owner/repo', 90, author='corsia[bot]', author_type='Bot')
    with (patch('events.platform.emit_product_event') as emit,
          patch('events.router.delivery.deliver_to_thread') as deliver):
        res = await route('webhook.github.pr_opened', payload)
    deliver.assert_not_called()
    assert res.outcome == RouteOutcome.SUPPRESSED
    assert _route_events(emit) == [('suppressed', {
        'internal_event': 'webhook.github.pr_opened',
        'author': 'corsia[bot]', 'rule_id': 'rule_set:github@1:self'})]


@pytest.mark.asyncio
async def test_two_rule_recipients_dead_letter_instead_of_first_wins(isolated_db):
    from events.router import RouteOutcome

    register_pr('owner/repo', 7, mk_session('owner'))
    extra = mk_session('extra')
    with write_transaction(None, scope='two_recipients') as db:
        _every_rule(db, 'extra-wake', to='thread:' + extra)
    with patch('events.router.delivery.deliver_to_thread') as deliver:
        res = await route('webhook.github.pr_merged', _merged())
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (RouteOutcome.DEAD_LETTERED, 'multiple_rule_recipients')


@pytest.mark.asyncio
async def test_inactive_tag_target_reason_comes_from_the_evaluator(isolated_db):
    from events.router import RouteOutcome

    register_pr('owner/repo', 7, mk_session('owner'))
    payload = {**_merged(), 'corsia_tags': [{'thread_id': 'gone'}]}
    with patch('events.router.delivery.deliver_to_thread') as deliver:
        res = await route('webhook.github.pr_merged', payload)
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (RouteOutcome.DEAD_LETTERED, 'invalid_tag_target')


def test_platform_drop_uses_the_same_drop_record(isolated_db):
    with write_transaction(None, scope='platform_drop') as db:
        db.execute('''INSERT INTO event_rules
            (id,created_at,updated_at,scope,connector,event_type,"match","on",action,"to",mode,precedence)
            VALUES ('stop','a','b','global','github','pr.merged','[]','every','drop',NULL,NULL,0)''')
    with patch('events.platform.emit_product_event') as emit:
        delivered = deliver_platform_event_sync('github', _merged(), 'pr.merged')
    assert [d.outcome for d in delivered] == ['dropped']
    assert [(action, props['rule_id']) for action, props in _route_events(emit)] == [
        ('suppressed', 'stop')]
