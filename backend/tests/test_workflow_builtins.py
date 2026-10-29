"""Workflow operands use task version and the source's period."""
import json

from core.corrections_recording import ACTIVE_VERSION_SETTING, ClassifierVersion, version_attributes, version_key
from infra.settings import set_setting
from core.measurement_scope import MeasurementScope
from core.workflow_builtin_operands import read_builtin
from tests.test_workflow_verdict_read import db, node, usage


def read(db, builtin, start='2026-10-01', end='2026-10-04', workflow='sdlc-process'):
    return read_builtin(db, builtin=builtin, workflow=workflow, user_id='steve',
                        scope=MeasurementScope(project='all', agent='root'), start=start, end=end)


def seed_correction(db, source, *, node_id='old', version='active', label='correction', classified='2026-10-03', suffix='1'):
    at = '2026-10-03T12:00:00Z'
    db.execute("INSERT OR IGNORE INTO messages (id,thread_id,role,content,created_at) VALUES (?,'root','user','x',?)",
               (source, at))
    db.execute("INSERT OR IGNORE INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,"
               "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES (?,'root',0,'m','completed','2026-10-03T11:00:00Z','boot','plan',?,'attempt',?,'execution')",
               ('turn-'+source, node_id, node_id))
    attrs = {**version_attributes(ClassifierVersion('asset', version, 'head', 0.5, 'policy')),
             'label': label, 'classified_at': classified}
    fact_id = source + version + suffix
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "agent_thread_id,attributes) VALUES (?,?,'measurement.correction','corsia','observed',?,?,?,'root',?)",
               (fact_id, fact_id, at, at, 'message:'+source, json.dumps(attrs)))


def activate(db):
    set_setting(ACTIVE_VERSION_SETTING, {'key': version_key(ClassifierVersion('asset','active','head',0.5,'policy'))}, db=db)


def test_spanning_period_and_version(db):
    node(db, 'old', version='v1', ended='2026-10-05T00:00:00Z')
    node(db, 'other', version='v2', ended='2026-10-03T00:00:00Z')
    node(db, 'open', version='v3', state='active')
    usage(db, 'u', 'old')
    activate(db)
    seed_correction(db, 'correction')
    assert read(db, 'corrections').versions['v1'].value == 1
    assert read(db, 'corrections', '2026-10-04', '2026-10-06').versions['v1'].value == 0
    assert read(db, 'runs').versions['v1'].value == 0
    assert read(db, 'runs').versions['v2'].value == 1
    assert read(db, 'runs').versions['v3'].value == 0
    assert read(db, 'fresh_tokens').versions['v1'].value == 1_000_000
    cost = read(db, 'cost_usd').versions['v1']
    assert cost.value is not None and cost.value > 0 and cost.unknown == 0
    assert read(db, 'runs', '2026-10-04', '2026-10-06').versions['v1'].value == 1


def test_usage_respects_half_hour_boundaries(db):
    node(db, 'old')
    for identity, at, prompt in [('before', '2026-10-02T00:29:59Z', 1_000_000),
                                 ('start', '2026-10-02T00:30:00Z', 2_000_000),
                                 ('inside', '2026-10-02T01:29:59Z', 3_000_000),
                                 ('end', '2026-10-02T01:30:00Z', 4_000_000)]:
        usage(db, identity, 'old', prompt=prompt)
        db.execute('UPDATE llm_usage SET created_at=? WHERE id=?', (at, identity))
    for builtin in ('fresh_tokens', 'cost_usd'):
        result = read(db, builtin, '2026-10-02T00:30:00Z', '2026-10-02T01:30:00Z').versions['v1']
        assert result.n == 2
        assert result.value and result.value > 0
    assert read(db, 'fresh_tokens', '2026-10-02T00:30:00Z', '2026-10-02T01:30:00Z').versions['v1'].value == 5_000_000
    selected = read(db, 'cost_usd', '2026-10-02T00:30:00Z', '2026-10-02T01:30:00Z').versions['v1'].value
    whole = read(db, 'cost_usd', '2026-10-02T00:00:00Z', '2026-10-02T02:00:00Z').versions['v1'].value
    assert selected == whole / 2


def test_only_latest_active_classifier_counts(db):
    node(db, 'old')
    activate(db)
    seed_correction(db, 'negative', classified='2026-10-03', suffix='a')
    seed_correction(db, 'negative', label='not_correction', classified='2026-10-04', suffix='b')
    seed_correction(db, 'negative', version='inactive', classified='2026-10-05')
    seed_correction(db, 'positive', classified='2026-10-03', suffix='a')
    seed_correction(db, 'positive', classified='2026-10-04', suffix='b')
    from core.correction_attribution import person_corrections
    result = read(db, 'corrections').versions['v1']
    assert result.value == 1 and result.n == 1
    assert len(person_corrections(db, '2026-10-01', '2026-10-04')) == result.value


def test_window_uses_sqlite_time_comparisons_in_one_query(db):
    from core.workflow_builtin_operands import _in_window

    queries = []
    db.set_trace_callback(queries.append)
    try:
        matches = _in_window(db, ['2026-10-01T01:00:00+01:00', '2026-10-03T23:59:59Z',
                                  '2026-10-04T00:00:00Z', 'invalid', None],
                             '2026-10-01T00:00:00Z', '2026-10-04T00:00:00Z')
    finally:
        db.set_trace_callback(None)
    assert matches == {0, 1}
    assert len(queries) == 1


def test_runs_use_first_terminal_instant_across_states(db):
    node(db, 'old', version='v1', ended='2026-10-05T00:00:00Z')
    from tests.plan_writer_authorizer import raw_plan_seed
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                   "VALUES ('earlier','old','test','failed','active','failed','2026-10-04T01:00:00+02:00')")
    assert read(db, 'runs').versions['v1'].value == 1
    assert read(db, 'runs', '2026-10-04', '2026-10-06').versions['v1'].value == 0


def test_untracked_and_unknown_cost(db):
    assert read(db, 'runs').reason == 'This workflow has no tasks'
    node(db, 'old')
    usage(db, 'u', 'old', model='not-a-listed-model', manifest=None)
    assert read(db, 'active_hours').reason == "Active time isn't recorded per task"
    cost = read(db, 'cost_usd').versions['v1']
    assert cost.value is None and cost.unknown == 1 and cost.n == 1
