"""The rule evaluator alone decides a webhook's recipient or drop."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.event_routing_helpers import (
    check_suite_payload, mk_session, pr_payload, register_pr, route,
)


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
          patch('events.router.emit_product_event') as emit,
          patch('events.router.delivery.deliver_to_thread') as deliver):
        res = await route('webhook.github.check_suite_completed',
                          check_suite_payload('o/r', 71, conclusion='cancelled'))
    lookup.assert_not_called()
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (router.RouteOutcome.SUPPRESSED, 'rule_set:github@1:cancelled')
    assert _route_events(emit) == [('suppressed', {
        'internal_event': 'webhook.github.check_suite_completed',
        'author': 'alice', 'rule_id': 'rule_set:github@1:cancelled'})]


@pytest.mark.asyncio
async def test_check_event_dropped_by_another_row_is_labeled_by_that_row(isolated_db):
    from events import router

    register_pr('o/r', 72, mk_session('wk-bot'))
    payload = check_suite_payload('o/r', 72, conclusion='success')
    payload['sender'] = {'login': 'ci[bot]', 'type': 'Bot'}
    with (patch.object(router, 'check_suite_pr_metadata_from_event') as lookup,
          patch('events.router.delivery.deliver_to_thread') as deliver):
        res = await route('webhook.github.check_suite_completed', payload)
    lookup.assert_not_called()
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (router.RouteOutcome.SUPPRESSED, 'rule_set:github@1:self')


@pytest.mark.asyncio
async def test_self_authored_drop_logs_product_event_with_author_and_rule(isolated_db):
    from events.router import RouteOutcome

    register_pr('owner/repo', 90, mk_session('owner'))
    payload = pr_payload('owner/repo', 90, author='corsia[bot]', author_type='Bot')
    with (patch('events.router.emit_product_event') as emit,
          patch('events.router.delivery.deliver_to_thread') as deliver):
        res = await route('webhook.github.pr_opened', payload)
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (RouteOutcome.SUPPRESSED, 'rule_set:github@1:self')
    assert _route_events(emit) == [('suppressed', {
        'internal_event': 'webhook.github.pr_opened',
        'author': 'corsia[bot]', 'rule_id': 'rule_set:github@1:self'})]


@pytest.mark.asyncio
async def test_inactive_tag_target_reason_comes_from_the_evaluator(isolated_db):
    from events.router import RouteOutcome

    register_pr('owner/repo', 7, mk_session('owner'))
    payload = {**_merged(), 'corsia_tags': [{'thread_id': 'gone'}]}
    with patch('events.router.delivery.deliver_to_thread') as deliver:
        res = await route('webhook.github.pr_merged', payload)
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (RouteOutcome.DEAD_LETTERED, 'invalid_tag_target')
