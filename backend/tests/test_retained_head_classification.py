"""The shared retained head keeps FIFO, model and account exclusions."""
import pytest

from test_clean_turn_terminal import _db, _session


@pytest.mark.parametrize('case,expected', [
    ('empty', None), ('null_only', None), ('mixed', 'account'),
    ('claimed_lower', 'account'), ('account', 'account'), ('model', 'model'),
    ('ordinary', None), ('unproved', None), ('wrong_target', None),
    ('delivered', None), ('earlier_plain', None), ('claimed_hold', None),
])
def test_retained_head_classification(case, expected):
    from infra.unavailable_model_recovery import held_head_kind_sql

    db = _db()
    try:
        _session(db, 'thread-1')
        rows = []

        def row(identity, order, *, error='account_signed_out', outcome='proved_no_write',
                target='claude-code/opus', turn=None, state='queued'):
            rows.append((identity, order, error, outcome, target, turn, state))

        if case == 'null_only':
            row('null', None)
        elif case == 'mixed':
            row('null', None)
            row('head', 2)
        elif case == 'claimed_lower':
            row('claimed', 1, turn='old-turn')
            row('head', 2)
        elif case == 'account':
            row('head', 1)
        elif case == 'model':
            row('head', 1, error='selected_model_unavailable')
        elif case == 'ordinary':
            row('head', 1, error=None, outcome=None)
        elif case == 'unproved':
            row('head', 1, outcome=None)
        elif case == 'wrong_target':
            row('head', 1, target='codex/other')
        elif case == 'delivered':
            row('head', 1, state='delivered')
        elif case == 'earlier_plain':
            row('plain', 1, error=None, outcome=None)
            row('head', 2)
        elif case == 'claimed_hold':
            row('head', 1, turn='old-turn')
        db.executemany("INSERT INTO messages(id,thread_id,role,content,created_at,"
                       "input_order,delivery_error,write_outcome,target_model,active_turn_id,delivery_state) "
                       "VALUES (?,'thread-1','user','input','t',?,?,?,?,?,?)", rows)
        assert db.execute(f"SELECT {held_head_kind_sql(chr(39) + 'thread-1' + chr(39))}").fetchone()[0] == expected
    finally:
        db.close()
