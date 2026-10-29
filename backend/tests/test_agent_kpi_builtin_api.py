"""Workflow builtins on the real agent KPI HTTP read."""
from infra.database import get_db
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_agent_kpi_api import client, definition, fact, item, period, read


def task(db, name, version):
    with raw_plan_seed(db):
        db.execute("INSERT OR IGNORE INTO plans(id,owner_kind,owner_id,created_at,updated_at) "
                   "VALUES ('plan','thread','owner','2026-01-01','2026-01-01')")
        db.execute("INSERT INTO plan_nodes(id,plan_id,kind,title,acceptance_criteria,order_key,commitment_id,"
                   "workflow_ref,workflow_version,created_at,updated_at) "
                   "VALUES (?,'plan','task','Task','done',1,?,'demo',?,'2026-01-01','2026-01-07')",
                   (name, name, version))
        db.execute("INSERT INTO plan_node_events(id,node_id,event,provenance,new_state,created_at) "
                   "VALUES (?,?,'accepted','test','accepted','2026-01-07')", ('end-'+name, name))


def test_builtin_ratio_and_rate_per_run(client):
    http, path = client
    db = get_db()
    definition(db, path, 'event', 'demo.done', {'attributes': {}})
    definition(db, path, 'kpi', 'ratio', {'formula': 'ratio', 'num': {'event': 'demo.done'},
                                         'den': {'builtin': 'runs'}, 'min_n': 1})
    definition(db, path, 'kpi', 'rate', {'formula': 'rate_per_run', 'num': {'event': 'demo.done'}, 'min_n': 1})
    task(db, 'one', 'hash1')
    task(db, 'two', 'hash2')
    fact(db, 'demo.done', 6, {'workflow_version': 'hash1'})
    fact(db, 'demo.done', 7, {'workflow_version': 'hash1'})
    db.commit()
    db.close()
    result = read(http)
    for name in ('ratio', 'rate'):
        row = period(result, name)
        assert row['value'] == 1 and row['n'] == 2
        by_version = {v['version']: v for v in row['versions']}
        assert by_version['hash1']['value'] == 2
        assert by_version['hash2']['value'] == 0
    assert {v['version'] for v in item(result, 'rate')['versions']} == {'hash1', 'hash2'}


def test_unknown_cost_stays_unknown_across_versions(client):
    http, path = client
    db = get_db()
    definition(db, path, 'kpi', 'cost', {'formula': 'count', 'num': {'builtin': 'cost_usd'}, 'min_n': 1})
    task(db, 'one', 'hash1')
    task(db, 'two', 'hash2')
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
               "cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,"
               "plan_commitment_id,plan_usage_role) VALUES ('u','owner','not-a-listed-model',100,0,0,0,100,"
               "'conversation','2026-01-06','plan','one','attempt','one','execution')")
    db.commit()
    db.close()
    row = period(read(http), 'cost')
    assert row['value'] is None and row['unknown'] == 1 and row['partial']
    assert next(v for v in row['versions'] if v['version'] == 'hash1')['value'] is None


def test_no_tasks_and_presence_untracked(client):
    http, path = client
    db = get_db()
    definition(db, path, 'kpi', 'runs', {'formula': 'count', 'num': {'builtin': 'runs'}, 'min_n': 1})
    definition(db, path, 'kpi', 'presence', {'formula': 'count', 'num': {'builtin': 'active_hours'}, 'min_n': 1})
    db.commit()
    assert period(read(http), 'runs')['status'] == 'Not tracked: this workflow has no tasks'
    task(db, 'one', 'hash')
    db.commit()
    db.close()
    row = period(read(http), 'presence')
    assert row['value'] is None and row['status'] == 'Not tracked: presence is not recorded per task'
