"""Exercise the agent KPI HTTP route with a realistic days-grain ledger."""
import json
import time

from infra.database import get_db
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_agent_kpi_api import client, definition


def test_days_route_with_matched_events_and_corrections(client):
    http, path = client
    db = get_db()
    definition(db, path, 'event', 'demo.run', {'attributes': {}})
    definition(db, path, 'event', 'demo.label', {'attributes': {}})
    match = {'event': 'demo.label', 'match': [{'event': 'demo.run', 'on': 'id', 'mode': 'exists'}]}
    definition(db, path, 'kpi', 'matched', {'formula': 'count', 'num': match, 'min_n': 1})
    definition(db, path, 'kpi', 'runs', {'formula': 'count', 'num': {'builtin': 'runs'}, 'min_n': 1})
    definition(db, path, 'kpi', 'corrections', {'formula': 'count', 'num': {'builtin': 'corrections'}, 'min_n': 1})
    definition(db, path, 'kpi', 'tokens', {'formula': 'count', 'num': {'builtin': 'fresh_tokens'}, 'min_n': 1})
    definition(db, path, 'kpi', 'cost', {'formula': 'count', 'num': {'builtin': 'cost_usd'}, 'min_n': 1})
    definition(db, path, 'kpi', 'cost_per_run', {'formula': 'ratio', 'num': {'builtin': 'cost_usd'},
                                                 'den': {'builtin': 'runs'}, 'min_n': 1})
    with raw_plan_seed(db):
        db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
                   "VALUES ('scale-plan','thread','owner','active',1,'2026-01-01','2026-01-01')")
        db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
                   "workflow_ref,workflow_version,requirements_revision,revision,created_at,updated_at) "
                   "VALUES ('scale-node','scale-plan',1,'task','scale','done','accepted','scale-node',"
                   "'demo','v1',1,1,'2026-01-01','2026-01-10')")
        db.executemany("INSERT INTO plan_node_events (id,node_id,provenance,event,new_state,created_at) "
                       "VALUES (?,'scale-node','test',?,?, '2026-01-10T12:00:00Z')",
                       [('terminal', 'accepted', 'accepted')] + [(f'r{i}', 'review_rejected', None) for i in range(5000)])
    rows = []
    for i in range(5000):
        for name in ('demo.run', 'demo.label'):
            key = f'{name}-{i}'
            rows.append((key, key, name, 'agent', 'agent_recorded', '2026-01-10T12:00:00Z',
                         '2026-01-10T12:00:00Z', None, 'p', 'owner', json.dumps({'id': str(i)})))
        key = f'correction-{i}'
        rows.append((key, key, 'measurement.correction', 'corsia', 'observed', '2026-01-10T12:00:00Z',
                     '2026-01-10T12:00:00Z', f'plan-review-rejected:r{i}', 'p', 'owner',
                     json.dumps({'source_kind': 'sent_back', 'source_id': f'r{i}', 'label': 'correction'})))
    db.executemany('INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,'
                   'subject_key,project_id,agent_thread_id,attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)
    db.executemany("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
                   "cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,"
                   "plan_commitment_id,plan_usage_role) VALUES (?,'owner',?,1000,100,0,0,1100,"
                   "'external_harness:claude-code',?,'scale-plan','scale-node','attempt','scale-node','execution')",
                   [(f'usage-{i}', ('claude-code/opus', 'claude-code/sonnet')[i % 2], f'2026-01-{1 + i % 28:02d}T12:00:00Z')
                    for i in range(5000)])
    db.commit()
    db.close()
    start = time.perf_counter()
    response = http.get('/agent-kpis', params={'project': 'p', 'agent': 'owner', 'timezone': 'UTC'})
    elapsed = time.perf_counter() - start
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result['kpis']) == 6
    for name, expected in [('matched', 5000), ('runs', 1), ('corrections', 5000)]:
        kpi = next(item for item in result['kpis'] if item['name'] == name)
        assert next(period for period in kpi['periods'] if period['period'] == '2026-01-10')['value'] == expected
    tokens = next(item for item in result['kpis'] if item['name'] == 'tokens')
    assert sum(period['value'] or 0 for period in tokens['periods']) > 0
    print(f'days route: {elapsed:.3f}s; 10000 events, 5000 corrections, 5000 usage rows, 6 KPIs')
