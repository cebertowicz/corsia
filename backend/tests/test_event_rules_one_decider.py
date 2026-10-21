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


def _envelope(**facts):
    from events.envelope import EventEnvelope

    return EventEnvelope('', 'webhook.github.pr_merged', 'github', facts, (),
                         {'integration': 'github', 'type': 'pr', 'external_id': 'o/r#7'},
                         {}, 'actionable', False)


def _row(row_id, to, action='deliver'):
    from events.rules import EventRule

    return EventRule(row_id, 'global', 'github', '*', [], 'every', action, to, 100, True)


def test_target_names_the_row_kind_that_matched():
    from events.resolve import Target
    from events.rules import TAG_RELATIONSHIP, decide

    owner = Target('o', 'path_a_resolve', 'owner')
    facts = {'corsia_tag': 'gone-or-not'}
    by_owner = decide([_row('r:owner', 'registered_owner')], _envelope(**facts),
                      resolve_owner=lambda: [owner])
    assert by_owner == [owner] and by_owner[0].relationship != TAG_RELATIONSHIP
    with patch('events.delivery.thread_exists_and_active', return_value=True):
        by_tag = decide([_row('r:tag', 'thread:$corsia_tag')], _envelope(**facts),
                        resolve_owner=lambda: [owner])
    assert (by_tag[0].thread, by_tag[0].relationship) == ('gone-or-not', TAG_RELATIONSHIP)


def test_route_label_follows_the_matched_row_not_the_payload_tag(isolated_db):
    from events import router
    from events.identity import resolve_identity
    from events.resolve import Target

    register_pr('owner/repo', 7, mk_session('owner'))
    payload = {**_merged(), 'corsia_tags': [{'thread_id': 'tagged'}]}
    identity = resolve_identity('webhook.github.pr_merged', payload)
    owner_row = [Target('owner', 'path_a_resolve', 'owner', routing_state={})]
    with patch.object(router, 'evaluate', return_value=owner_row):
        selected = router._select_route('webhook.github.pr_merged', payload, identity,
                                        identity.external_id, None, False)
    assert selected[:2] == ('owner', 'owner')


def test_rule_table_without_a_deliver_row_dead_letters_with_its_own_reason():
    from events.rules import decide

    [target] = decide([], _envelope(), resolve_owner=lambda: [])
    assert (target.thread, target.reason, target.disposition) == (
        None, 'rule_table_invalid', 'dead_letter')


@pytest.mark.asyncio
async def test_invalid_rule_table_is_dead_lettered_as_rule_table_invalid(isolated_db):
    from events import router
    from events.resolve import Target

    register_pr('owner/repo', 7, mk_session('owner'))
    invalid = [Target(None, 'rule', 'none', reason='rule_table_invalid',
                      disposition='dead_letter')]
    with (patch.object(router, 'evaluate', return_value=invalid),
          patch('events.router.delivery.deliver_to_thread') as deliver):
        res = await route('webhook.github.pr_merged', _merged())
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (router.RouteOutcome.DEAD_LETTERED, 'rule_table_invalid')


@pytest.mark.asyncio
async def test_first_seen_rule_does_not_bind_a_first_seen_webhook_artifact(isolated_db):
    from connections import binder
    from events import router
    from registry import models

    target = mk_session('rule-target')
    binder.create_rule(connector='github', fact='repo', value='owner/repo',
                       destination_thread_id=target)
    with patch('events.router.delivery.deliver_to_thread') as deliver:
        res = await route('webhook.github.pr_merged', _merged())
    deliver.assert_not_called()
    assert (res.outcome, res.reason) == (router.RouteOutcome.DEAD_LETTERED, 'no_owner')
    state = models.routing_registrations('github', 'pr', 'owner/repo#7')
    assert state['owner'] is None
