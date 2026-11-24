"""Ranking authority, and at most one unread ranking wake per ranker."""
import json
from plans import service
from tests.test_attention_queue import db  # noqa: F401
from tests.test_attention_queue_api import _DbProxy


def seed(db):
    plan = service.ensure_plan('thread', 'root', db=db)
    node = service.add_node(plan.id, kind='task', title='Person task',
                            acceptance_criteria='Done', assignee='user', db=db)
    db.execute("UPDATE projects SET conductor_thread_id='root' WHERE id='project-1'")
    db.commit()
    return f'task:{node.id}'


def test_rank_tool_is_loadable_from_real_harness_registry(tmp_path):
    from subagents.harness_tool_bridge import build_tool_registry
    from tests.test_agent_tool_surface import _loaded_schemas

    registry = build_tool_registry(str(tmp_path))
    schema = _loaded_schemas(registry, 'automations')['attention_rank']
    assert schema == registry.get('attention_rank').to_schema()['function']
    assert schema['parameters']['properties']['operation']['enum'] == ['read', 'set']


def test_ranker_set_is_authorized_atomic_and_replaces(db, monkeypatch):
    from tools import attention_rank
    key = seed(db)
    monkeypatch.setattr(attention_rank, 'get_db', lambda: _DbProxy(db))
    actor = {'user_id':'user-1', 'project_id':'project-1'}
    assert 'Parent work ranks' in attention_rank._run('child', actor, 'set', [{'key':key,'reason':'Ready'}])
    from infra.activity_ranking import REASON_MAX_CHARS
    assert f'exceeds {REASON_MAX_CHARS}' in attention_rank._run('root', actor, 'set', [{'key':key,'reason':'x'*(REASON_MAX_CHARS + 1)}])
    assert 'Ranked 1' in attention_rank._run('root', actor, 'set', [{'key':key,'reason':'Ready'}])
    assert 'Unknown key' in attention_rank._run('root', actor, 'set', [{'key':'stale','reason':'Wrong'}])
    assert db.execute('SELECT item_key, reason FROM activity_ranks').fetchone()[:] == (key, 'Ready')
    read = json.loads(attention_rank._run('root', actor, 'read', None))
    assert read['items'][0]['rank']['reason'] == 'Ready'


def test_set_composes_the_queue_outside_the_write_lock(db, monkeypatch):
    from api import attention
    from tools import attention_rank
    key = seed(db)
    monkeypatch.setattr(attention_rank, 'get_db', lambda: _DbProxy(db))
    actual_queue = attention.activity_queue
    def read_without_write_lock(connection, *args, **kwargs):
        assert not connection.in_transaction
        return actual_queue(connection, *args, **kwargs)
    monkeypatch.setattr(attention, 'activity_queue', read_without_write_lock)
    events = []
    monkeypatch.setattr(attention_rank, 'emit_product_event', lambda *a, **k: events.append((a, k['properties'])))
    actor = {'user_id':'user-1', 'project_id':'project-1'}
    assert 'Ranked 1' in attention_rank._run('root', actor, 'set', [{'key':key,'reason':'Ready'}])
    assert events == [(('activity_rank', 'set'), {'scope_kind':'project', 'ranked':1, 'unranked':0})]


def test_set_refuses_when_the_ranker_changes_before_the_write(db, monkeypatch):
    from api import attention
    from tools import attention_rank
    key = seed(db)
    monkeypatch.setattr(attention_rank, 'get_db', lambda: _DbProxy(db))
    actual_queue = attention.activity_queue
    def reassign_during_read(connection, *args, **kwargs):
        result = actual_queue(connection, *args, **kwargs)
        connection.execute("UPDATE projects SET conductor_thread_id='child' WHERE id='project-1'")
        connection.commit()
        return result
    monkeypatch.setattr(attention, 'activity_queue', reassign_during_read)
    result = attention_rank._run('root', {'user_id':'user-1', 'project_id':'project-1'}, 'set', [{'key':key,'reason':'Ready'}])
    assert result.startswith('Error:') and 'not you' in result
    assert db.execute('SELECT count(*) FROM activity_ranks').fetchone()[0] == 0


def test_read_is_capped_like_the_activity_list_and_counts_everything(db, monkeypatch):
    from api import attention
    from tools import attention_rank
    seed(db)
    _add_task(db, '2026-01-01T00:01:00+00:00')
    monkeypatch.setattr(attention_rank, 'get_db', lambda: _DbProxy(db))
    monkeypatch.setattr(attention, 'QUEUE_ITEMS_CAP', 1)
    read = json.loads(attention_rank._run('root', {'user_id':'user-1', 'project_id':'project-1'}, 'read', None))
    assert read['count'] == 2 and len(read['items']) == 1


def test_no_ranker_refusal_says_no_thread_ranks_the_scope(db, monkeypatch):
    from tools import attention_rank
    monkeypatch.setattr(attention_rank, 'ranker_for', lambda *_a, **_k: ('workspace', None))
    result = attention_rank._run('root', {'user_id':'user-1', 'project_id':None}, 'read', None)
    assert result == 'Error: no thread ranks this scope; it has no conductor or workspace agent.'


def _unread_wakes(db):
    return db.execute("SELECT count(*) FROM messages WHERE thread_id='root' AND delivery_state='queued' "
                      "AND write_started_at IS NULL "
                      "AND json_extract(metadata,'$.event_type')='activity.unranked'").fetchone()[0]


def _read_wakes(db):
    db.execute("UPDATE messages SET write_started_at='2026-01-01T00:00:01+00:00' WHERE thread_id='root' "
               "AND json_extract(metadata,'$.event_type')='activity.unranked'")
    db.commit()


def _add_task(db, created_at):
    plan = service.ensure_plan('thread', 'root', db=db)
    node = service.add_node(plan.id, kind='task', title='New', acceptance_criteria='Done', assignee='user', db=db)
    db.execute('UPDATE plan_nodes SET created_at=? WHERE id=?', (created_at, node.id))
    db.commit()
    return f'task:{node.id}'


def test_no_second_wake_while_the_first_is_unread_and_items_raised_meanwhile_wake_once_after(db, monkeypatch):
    from api import attention
    from automation.continuation.sources import activity_ranking

    key = seed(db)
    db.execute('UPDATE plan_nodes SET created_at=? WHERE id=?',
               ('2025-12-31T23:59:00+00:00', key.removeprefix('task:')))
    db.commit()
    actual_queue = attention.activity_queue
    def read_without_write_lock(connection, *args, **kwargs):
        assert not connection.in_transaction
        return actual_queue(connection, *args, **kwargs)
    monkeypatch.setattr(attention, 'activity_queue', read_without_write_lock)
    source = activity_ranking.ActivityRankingSource()
    assert source.prepare('2026-01-01T00:00:00+00:00', db=db) == ['root']
    # Unread: a new item raised now does not stack a second wake.
    _add_task(db, '2026-01-01T00:01:00+00:00')
    assert source.prepare('2026-01-01T00:02:00+00:00', db=db) == []
    assert _unread_wakes(db) == 1
    # Read, with an item raised since the last wake: exactly one more wake.
    _read_wakes(db)
    assert source.prepare('2026-01-01T00:03:00+00:00', db=db) == ['root']
    assert source.prepare('2026-01-01T00:04:00+00:00', db=db) == []
    # Read, nothing raised since: the ranker is not woken again for items it already saw.
    _read_wakes(db)
    assert source.prepare('2026-01-01T00:05:00+00:00', db=db) == []
    assert _unread_wakes(db) == 0


def test_a_ranked_queue_sends_no_wake(db):
    from automation.continuation.sources import activity_ranking
    key = seed(db)
    db.execute('INSERT INTO activity_ranks VALUES (?,?,?,?,?,?,?)',
               ('user-1','project-1',key,1,'Ready','root','2026-01-01'))
    db.commit()
    assert activity_ranking.ActivityRankingSource().prepare('2026-01-01T00:00:00+00:00', db=db) == []
    assert _unread_wakes(db) == 0


def test_concurrent_nudge_recheck_prevents_duplicate(db, monkeypatch):
    from api import attention
    from automation.continuation.sources import activity_ranking

    seed(db)
    def nudge_during_read(connection, *args, **kwargs):
        result = original_queue(connection, *args, **kwargs)
        connection.execute('INSERT INTO activity_rank_nudges(user_id,scope,last_enqueued_at) VALUES (?,?,?)',
                           ('user-1', 'project-1', '2026-01-01T00:00:00+00:00'))
        connection.commit()
        return result
    original_queue = attention.activity_queue
    monkeypatch.setattr(attention, 'activity_queue', nudge_during_read)
    wakes = []
    monkeypatch.setattr(activity_ranking, 'enqueue_event_wake', lambda *args, **kwargs: wakes.append(args))
    assert activity_ranking.ActivityRankingSource().prepare('2026-01-01T00:00:00+00:00', db=db) == []
    assert wakes == []


def test_nudge_enqueues_real_system_input(db):
    from automation.continuation.sources.activity_ranking import ActivityRankingSource
    seed(db)
    assert ActivityRankingSource().prepare('2026-01-01T00:00:00+00:00', db=db) == ['root']
    row = db.execute("SELECT content FROM messages WHERE thread_id='root' AND delivery_state='queued' "
                     "AND json_extract(metadata,'$.event_type')='activity.unranked'").fetchone()
    assert row and 'attention_rank' in row['content']


def test_the_runners_own_sweep_with_no_connection_wakes_the_ranker(db, monkeypatch):
    """The continuation loop sweeps with db=None; reads then take the pooled connection."""
    import infra.database
    from automation.continuation import runner
    from automation.continuation.sources import activity_ranking
    seed(db)
    pooled = lambda: _DbProxy(db)  # noqa: E731
    for module in (infra.database, runner, activity_ranking):
        monkeypatch.setattr(module, 'get_db', pooled)
    errors = []
    monkeypatch.setattr(runner.logger, 'exception', lambda *a, **k: errors.append(a))
    woken = runner._prepare('2026-01-01T00:00:00+00:00', [activity_ranking.ActivityRankingSource()], None)
    assert errors == [] and woken == ['root']
    assert _unread_wakes(db) == 1


def test_nudge_failed_enqueue_does_not_throttle(db, monkeypatch):
    from automation.continuation.sources import activity_ranking
    seed(db)
    def fail(*args, **kwargs):
        raise RuntimeError('delivery failed')
    monkeypatch.setattr(activity_ranking, 'enqueue_event_wake', fail)
    assert activity_ranking.ActivityRankingSource().prepare('2026-01-01T00:00:00+00:00', db=db) == []
    assert db.execute('SELECT count(*) FROM activity_rank_nudges').fetchone()[0] == 0


def test_one_failed_scope_does_not_skip_the_next(db, monkeypatch):
    from automation.continuation.sources import activity_ranking
    monkeypatch.setattr(activity_ranking, '_scopes', lambda db: [
        ('user-1', 'bad', 'first'), ('user-1', 'good', 'second'),
    ])
    def enqueue(read_db, db, user_id, project_id, thread_id, now):
        if project_id == 'bad':
            raise RuntimeError('scope failed')
        return True
    monkeypatch.setattr(activity_ranking, '_enqueue_if_due', enqueue)
    assert activity_ranking.ActivityRankingSource().prepare('2026-01-01T00:00:00+00:00', db=db) == ['second']


def test_paused_ranker_is_not_woken_and_wakes_once_after_unpause(db, monkeypatch):
    from automation.continuation.sources import activity_ranking
    seed(db)
    paused = {'root': '2026-01-01T00:00:00+00:00'}
    monkeypatch.setattr(activity_ranking, 'thread_paused_since', lambda _db, thread: paused.get(thread))
    source = activity_ranking.ActivityRankingSource()
    assert source.prepare('2026-01-01T00:00:00+00:00', db=db) == []
    assert source.prepare('2026-01-01T01:00:00+00:00', db=db) == []
    assert db.execute('SELECT count(*) FROM activity_rank_nudges').fetchone()[0] == 0
    paused.clear()
    assert source.prepare('2026-01-01T02:00:00+00:00', db=db) == ['root']
    assert source.prepare('2026-01-01T02:01:00+00:00', db=db) == []
    assert _unread_wakes(db) == 1


def test_a_scope_that_keeps_failing_logs_once_until_it_changes(db, monkeypatch):
    from automation.continuation.sources import activity_ranking
    monkeypatch.setattr(activity_ranking, '_failing', {})
    monkeypatch.setattr(activity_ranking, '_scopes', lambda db: [('user-1', 'bad', 'first')])
    errors, infos = [], []
    monkeypatch.setattr(activity_ranking.logger, 'exception', lambda *a, **k: errors.append(a))
    monkeypatch.setattr(activity_ranking.logger, 'info', lambda *a, **k: infos.append(a))
    failure = {'text': 'scope failed'}
    def enqueue(*_args):
        if failure['text']:
            raise RuntimeError(failure['text'])
        return False
    monkeypatch.setattr(activity_ranking, '_enqueue_if_due', enqueue)
    source = activity_ranking.ActivityRankingSource()
    for _ in range(3):
        source.prepare('2026-01-01T00:00:00+00:00', db=db)
    assert len(errors) == 1
    failure['text'] = 'another failure'
    source.prepare('2026-01-01T00:01:00+00:00', db=db)
    assert len(errors) == 2
    failure['text'] = ''
    source.prepare('2026-01-01T00:02:00+00:00', db=db)
    assert len(infos) == 1 and activity_ranking._failing == {}


def test_an_old_task_that_falls_due_after_the_last_wake_wakes_the_ranker(db):
    from automation.continuation.sources import activity_ranking
    seed(db)
    plan = service.ensure_plan('thread', 'root', db=db)
    waiting = service.add_node(plan.id, kind='task', title='Later step', acceptance_criteria='Done',
                               assignee='user', later=True, db=db)
    db.execute('UPDATE plan_nodes SET created_at=? WHERE id=?', ('2025-12-01T00:00:00+00:00', waiting.id))
    db.commit()
    source = activity_ranking.ActivityRankingSource()
    assert source.prepare('2026-01-01T00:00:00+00:00', db=db) == ['root']
    _read_wakes(db)
    assert source.prepare('2026-01-01T00:01:00+00:00', db=db) == []
    # Created long before the last wake, it is new to the queue once it leaves Later.
    db.execute('UPDATE plan_nodes SET later=0 WHERE id=?', (waiting.id,))
    db.commit()
    assert source.prepare('2026-01-01T00:02:00+00:00', db=db) == ['root']
    seen = json.loads(db.execute('SELECT seen_keys FROM activity_rank_nudges').fetchone()[0])
    assert f'task:{waiting.id}' in seen


def test_an_item_that_leaves_and_comes_back_unranked_after_a_set_wakes_the_ranker(db, monkeypatch):
    from automation.continuation.sources import activity_ranking
    from tools import attention_rank
    key = seed(db)
    monkeypatch.setattr(attention_rank, 'get_db', lambda: _DbProxy(db))
    node_id = key.split(':', 1)[1]
    source = activity_ranking.ActivityRankingSource()
    assert source.prepare('2026-01-01T00:00:00+00:00', db=db) == ['root']
    _read_wakes(db)
    # The item goes into Later; the ranker's set reads the queue without it, dropping its rank.
    db.execute('UPDATE plan_nodes SET later=1 WHERE id=?', (node_id,))
    db.commit()
    assert 'Ranked 0' in attention_rank._run('root', {'user_id':'user-1', 'project_id':'project-1'}, 'set', [])
    db.execute('UPDATE plan_nodes SET later=0 WHERE id=?', (node_id,))
    db.commit()
    # Back in the queue unranked, it is new again.
    assert source.prepare('2026-01-01T00:01:00+00:00', db=db) == ['root']
