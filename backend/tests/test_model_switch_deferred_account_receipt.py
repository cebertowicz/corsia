"""Account-move receipts follow the terminal model-switch outcome."""
from __future__ import annotations

import json

from connections import harness_usage
from connections.account_evidence import mark_discovery
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt
from connections.provider_policy import effective_policy, set_policy
from connections.vendor_accounts import add_account, default_account, set_account_state
from infra.database import write_transaction
from vendor_account_fixtures import accounts, vendors  # noqa: F401


def _receipt_rows(db):
    return db.execute(
        "SELECT metadata FROM messages WHERE json_extract(metadata, '$.type')='account_switch'",
    ).fetchall()


def _configure_cross_vendor_destination(db):
    db.execute(
        "INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
        "VALUES ('claude-code/target','Claude target','claude-code/target','claude-code','target','claude-code','t','t')",
    )
    db.execute("UPDATE models SET context_window=4096 WHERE id='claude-code/target'")
    db.commit()
    first = default_account('claude-code')
    second = add_account('claude-code', 'Claude rollover')
    second = set_account_state(second['id'], 'signed_in', expected_revision=0)
    for account in (first, second):
        receipt = capture_receipt('claude-code', account_id=account['id'], db=db)
        assert receipt is not None
        assert mark_discovery(account['id'], receipt=receipt, status='ready',
                              models={'claude-code/target': {'context_window': 4096}}, db=db)
    policy = effective_policy('destination-project', 'claude-code', db=db)
    set_policy('destination-project', 'claude-code', rollover=True,
               order=[first['id'], second['id']], disabled=[],
               expected_revision=policy['revision'], db=db)
    select_account('destination', 'claude-code', account_id=first['id'], db=db)
    db.commit()
    with write_transaction(db, scope='configure_cross_vendor_destination'):
        harness_usage.record_limited(
            provider='claude-code', receipt=capture_receipt('claude-code', account_id=first['id'], db=db),
            reason='usage_limit', resets_at='2099-01-01T00:00:00+00:00',
            source='rate_limit_update', db=db,
        )
        harness_usage.record_allowed(
            provider='claude-code', receipt=capture_receipt('claude-code', account_id=second['id'], db=db),
            source='discovery', db=db,
        )
    return first, second


def _request_cross_vendor_switch(db, operation_id='switch-1'):
    from infra.model_switches import request_switch

    return request_switch('destination', 'claude-code/target', operation_id=operation_id,
                          requested_at='t1', user_id='user', db=db)


def test_cross_vendor_rollover_waits_to_receipt_until_switch_commits(vendors, monkeypatch):
    from connections import account_moves
    from infra.model_switches import activate_ready_switch

    db, _ = vendors
    first, second = _configure_cross_vendor_destination(db)
    published = []
    monkeypatch.setattr(account_moves, '_publish', published.append)

    from connections.account_rollover import rollover_destination
    from llm.model_status import harness_base_row

    assert rollover_destination('claude-code', first['id'], harness_base_row('claude-code/target', db=db),
                                'destination-project', db=db) == second['id']
    _request_cross_vendor_switch(db)

    assert _receipt_rows(db) == []
    deferred = db.execute(
        "SELECT deferred_move FROM thread_vendor_accounts WHERE thread_id='destination' AND provider='claude-code'",
    ).fetchone()['deferred_move']
    assert json.loads(deferred) == {
        'reason': 'account_limit',
        'from_account_id': first['id'], 'from_account_name': first['name'],
        'to_account_id': second['id'], 'to_account_name': second['name'],
        'resets_at': None,
    }

    with write_transaction(db, scope='test_switch_commit') as connection:
        connection.execute("UPDATE threads SET switch_status='ready' WHERE id='destination'")
        assert activate_ready_switch(connection, 'destination', committed_at='t2') is not None

    receipts = _receipt_rows(db)
    assert len(receipts) == 1
    metadata = json.loads(receipts[0]['metadata'])
    assert metadata['reason'] == 'account_limit'
    assert (metadata['from_account_id'], metadata['to_account_id']) == (first['id'], second['id'])
    assert len(published) == 1
    assert db.execute(
        "SELECT deferred_move FROM thread_vendor_accounts WHERE thread_id='destination' AND provider='claude-code'",
    ).fetchone()['deferred_move'] is None


def test_failed_cross_vendor_switch_never_posts_its_deferred_receipt(vendors, monkeypatch):
    from connections import account_moves
    from infra.model_switches import fail_switch

    db, _ = vendors
    _configure_cross_vendor_destination(db)
    monkeypatch.setattr(account_moves, '_publish', lambda _receipt: None)

    _request_cross_vendor_switch(db)
    fail_switch('switch-1', error='destination refused', db=db)

    assert _receipt_rows(db) == []
    assert db.execute(
        "SELECT deferred_move FROM thread_vendor_accounts WHERE thread_id='destination' AND provider='claude-code'",
    ).fetchone()['deferred_move'] is not None


def test_retry_after_a_failed_switch_posts_the_deferred_receipt_once(vendors, monkeypatch):
    from connections import account_moves
    from infra.model_switches import activate_ready_switch, fail_switch

    db, _ = vendors
    published = []
    monkeypatch.setattr(account_moves, '_publish', published.append)
    first, second = _configure_cross_vendor_destination(db)
    _request_cross_vendor_switch(db)
    fail_switch('switch-1', error='destination refused', db=db)
    _request_cross_vendor_switch(db, operation_id='switch-2')
    with write_transaction(db, scope='test_retry_commit') as connection:
        connection.execute("UPDATE threads SET switch_status='ready' WHERE id='destination'")
        assert activate_ready_switch(connection, 'destination', committed_at='t3') is not None

    receipts = _receipt_rows(db)
    assert len(receipts) == 1
    metadata = json.loads(receipts[0]['metadata'])
    assert (metadata['from_account_id'], metadata['to_account_id']) == (first['id'], second['id'])
    assert len(published) == 1


def test_switch_from_a_non_harness_model_defers_the_move_receipt(vendors, monkeypatch):
    from connections import account_moves

    db, _ = vendors
    monkeypatch.setattr(account_moves, '_publish', lambda _receipt: None)
    _configure_cross_vendor_destination(db)
    db.execute("UPDATE threads SET model='external@local/plain' WHERE id='destination'")
    db.commit()

    _request_cross_vendor_switch(db)

    assert _receipt_rows(db) == []
    assert db.execute(
        "SELECT deferred_move FROM thread_vendor_accounts WHERE thread_id='destination' AND provider='claude-code'",
    ).fetchone()['deferred_move'] is not None


def test_replaced_same_vendor_destination_releases_the_original_move(vendors, monkeypatch):
    from connections import account_moves
    from infra.model_switches import activate_ready_switch, request_switch

    db, _ = vendors
    published = []
    monkeypatch.setattr(account_moves, '_publish', published.append)
    first, second = _configure_cross_vendor_destination(db)
    _request_cross_vendor_switch(db)
    db.execute(
        "INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
        "VALUES ('claude-code/replacement','Claude replacement','claude-code/replacement','claude-code','replacement','claude-code','t','t')",
    )
    db.commit()
    request_switch('destination', 'claude-code/replacement', operation_id='switch-2',
                   requested_at='t2', user_id='user', db=db)
    with write_transaction(db, scope='test_replacement_commit') as connection:
        connection.execute("UPDATE threads SET switch_status='ready' WHERE id='destination'")
        assert activate_ready_switch(connection, 'destination', committed_at='t2') is not None

    receipts = _receipt_rows(db)
    assert len(receipts) == 1
    metadata = json.loads(receipts[0]['metadata'])
    assert (metadata['from_account_id'], metadata['to_account_id']) == (first['id'], second['id'])
    assert len(published) == 1


def test_same_vendor_rollover_still_posts_its_receipt_immediately(accounts, monkeypatch):
    from connections import account_moves
    from infra.model_switches import request_switch
    from vendor_account_fixtures import _configured

    db, first, second, _events = _configured(accounts, monkeypatch)
    db.execute(
        "INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
        "VALUES ('codex/target','Codex target','codex/target','codex','target','codex','t','t')",
    )
    db.execute("UPDATE models SET context_window=4096 WHERE id='codex/target'")
    db.commit()
    with write_transaction(db, scope='discover_same_vendor_target'):
        for account in (first, second):
            receipt = capture_receipt('codex', account_id=account['id'], db=db)
            assert receipt is not None
            assert mark_discovery(account['id'], receipt=receipt, status='ready',
                                  models={'codex/target': {'context_window': 4096}}, db=db)
    published = []
    monkeypatch.setattr(account_moves, '_publish', published.append)

    request_switch('destination', 'codex/target', operation_id='switch-1', requested_at='t1', user_id='user', db=db)

    receipts = _receipt_rows(db)
    assert len(receipts) == 1
    assert json.loads(receipts[0]['metadata'])['reason'] == 'account_limit'
    assert len(published) == 1
