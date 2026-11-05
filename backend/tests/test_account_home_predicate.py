"""A custom account's missing source home reads the same code before a send and at launch.

The readiness evaluator and ``validate_launch`` share ``missing_home_reason``,
so a home launch would refuse never lifts a hold.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from connections.account_evidence import discovery_view, mark_discovery, readiness_reason
from connections.account_holds import refresh_thread_holds
from connections.account_home import missing_home_reason
from connections.account_policy import AccountAdmissionError, select_account
from connections.account_receipts import capture_receipt
from connections.account_runtime import LaunchHomes, homes_for, validate_launch
from connections.vendor_accounts import default_account, get_account
from infra.database import write_transaction
from vendor_account_fixtures import accounts, signed_in_default, vendors  # noqa: F401

CODEX_MISSING = 'codex_auth_source_missing'


def _discovered(db, provider, row, model):
    receipt = capture_receipt(provider, account_id=row['id'], db=db)
    assert mark_discovery(row['id'], receipt=receipt, status='ready', models={model: {'available': True}}, db=db)
    db.commit()
    return receipt, get_account(row['id'], db=db)


def _with_home(provider, row):
    source = Path(row['source_home'])
    source.mkdir(parents=True, exist_ok=True)
    if provider == 'codex':
        source.joinpath('auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'x'}}))


def _launch_reason(db, account, receipt, model):
    try:
        validate_launch(account, launch=homes_for(account), receipt=receipt, model=model, db=db)
    except AccountAdmissionError as exc:
        return exc.reason
    return None


@pytest.mark.parametrize('provider,expected', [('claude-code', 'account_home_mismatch'), ('codex', 'account_home_mismatch')])
def test_missing_home_reads_the_same_code_from_evaluator_and_launch(vendors, provider, expected):  # noqa: F811
    db, rows = vendors
    model = provider + '/test'
    _with_home(provider, rows[provider])
    receipt, account = _discovered(db, provider, rows[provider], model)
    shutil.rmtree(account['source_home'])

    assert missing_home_reason(account) == expected
    assert readiness_reason(account, model=model) == expected
    assert _launch_reason(db, account, receipt, model) == expected


def test_codex_missing_auth_file_is_held_before_launch(vendors):  # noqa: F811
    db, rows = vendors
    _with_home('codex', rows['codex'])
    receipt, account = _discovered(db, 'codex', rows['codex'], 'codex/test')
    (Path(account['source_home']) / 'auth.json').unlink()
    assert readiness_reason(account, model='codex/test') == CODEX_MISSING
    assert _launch_reason(db, account, receipt, 'codex/test') == CODEX_MISSING
    # Launch refuses by the home rule, before the projection: no stored failure outlives the file.
    assert discovery_view(get_account(account['id'], db=db))['auth_projection']['failed'] is None


@pytest.mark.parametrize('provider', ['claude-code', 'codex'])
def test_present_home_passes_the_predicate(vendors, provider):  # noqa: F811
    db, rows = vendors
    _with_home(provider, rows[provider])
    assert missing_home_reason(get_account(rows[provider]['id'], db=db)) is None


def test_present_claude_home_passes_evaluator_and_launch(vendors):  # noqa: F811
    db, rows = vendors
    _with_home('claude-code', rows['claude-code'])
    receipt, account = _discovered(db, 'claude-code', rows['claude-code'], 'claude-code/test')
    assert readiness_reason(account, model='claude-code/test') is None
    assert _launch_reason(db, account, receipt, 'claude-code/test') is None


def test_the_default_is_judged_by_the_home_predicate_like_any_account(vendors):  # noqa: F811
    db, _ = vendors
    for provider in ('claude-code', 'codex'):
        default = signed_in_default(provider, db=db)
        assert default['source_home'] is not None
        assert missing_home_reason(default) is None


def test_hold_row_carries_the_missing_home_reason(vendors):  # noqa: F811
    db, rows = vendors
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) "
               "VALUES ('claude-code/test','Test','claude-code/test','claude-code','test','claude-code','t','t')")
    db.execute("UPDATE threads SET model='claude-code/test' WHERE id='destination'")
    db.commit()
    row = rows['claude-code']
    _with_home('claude-code', row)
    _discovered(db, 'claude-code', row, 'claude-code/test')
    select_account('destination', 'claude-code', account_id=row['id'], db=db)
    hold = ("SELECT reason FROM thread_vendor_account_holds WHERE thread_id='destination' AND provider='claude-code'")
    with write_transaction(db, scope='test_home_hold'):
        refresh_thread_holds('destination', wake=False, db=db)
    assert db.execute(hold).fetchone() is None

    shutil.rmtree(row['source_home'])
    with write_transaction(db, scope='test_home_hold'):
        refresh_thread_holds('destination', wake=False, db=db)
    assert db.execute(hold).fetchone()['reason'] == 'account_home_mismatch'
