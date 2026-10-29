"""KPI and verdict reads share the workflow's rules."""
import json
from datetime import datetime, timezone

import pytest

from api.measurement_agent_kpis import router  # Import the route as in the application.
from core.measurement_scope import MeasurementScope
from core.workflow_builtin_operands import read_builtin
from core.workflow_verdict_read import read_snapshot
from infra.measure_registry import sync
from tests.test_measure_registry import db as registry_db, own, small
from tests.test_workflow_builtins import activate, seed_correction
from tests.test_workflow_verdict_read import db, live_workflow_owners, node


def test_verdict_and_builtins_agree_on_workflow_runs_and_corrections(db):
    node(db, 'old', version='v1', ended='2026-10-03T00:00:00Z')
    activate(db)
    seed_correction(db, 'one')
    seed_correction(db, 'one', label='not_correction', classified='2026-10-04', suffix='2')
    seed_correction(db, 'two')
    snapshot = read_snapshot(db, now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    run = snapshot.workflows[0].runs[0]
    kwargs = dict(workflow='sdlc-process', user_id='steve', scope=MeasurementScope('all', 'root'),
                  start='2026-10-01', end='2026-10-04')
    assert read_builtin(db, builtin='runs', **kwargs).versions[run.version].value == 1
    assert read_builtin(db, builtin='corrections', **kwargs).versions[run.version].value == sum(f.is_correction for f in run.corrections)


def test_verdict_counts_classified_and_sent_back_corrections_as_the_builtin_does(db):
    from core.workflow_verdicts import BUILTIN_WINDOW_DAYS, CORRECTIONS, PlannedMeasure, _counter, _sample

    node(db, 'old', version='v1', ended='2026-10-03T00:00:00Z')
    activate(db)
    seed_correction(db, 'one')
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at) "
               "VALUES ('r','old','test','review_rejected','{}','2026-10-03T06:00:00Z')")
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "attributes) VALUES ('sb','sb','measurement.correction','corsia','observed','2026-10-03',"
               "'2026-10-03','plan-review-rejected:r',?)", (json.dumps({'source_kind': 'sent_back', 'label': 'correction'}),))
    run = read_snapshot(db, now=datetime(2026, 10, 19, tzinfo=timezone.utc)).workflows[0].runs[0]
    measure = PlannedMeasure(CORRECTIONS, 'quality', 'lower', 'builtin', BUILTIN_WINDOW_DAYS, floor=0.02)
    counter = _counter(measure, [run])
    assert counter not in (None, False)
    kwargs = dict(workflow='sdlc-process', user_id='steve', scope=MeasurementScope('all', 'root'),
                  start='2026-10-01', end='2026-10-04')
    assert _sample(measure, run, counter).num == read_builtin(db, builtin='corrections', **kwargs).versions['v1'].value == 2


def test_phase_descendants_share_one_run_and_corrections(db):
    from tests.plan_writer_authorizer import raw_plan_seed
    from tests.test_workflow_verdict_read import correction, usage

    node(db, 'phase')
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET kind='phase' WHERE id='phase'")
    node(db, 'step-a', parent='phase', workflow=None)
    node(db, 'step-b', parent='phase', workflow=None)
    node(db, 'nested', parent='step-a', workflow='sdlc-process')
    correction(db, 'phase-correction', 'phase', at='2026-10-06T00:00:00Z')
    correction(db, 'child-correction', 'step-a', at='2026-10-07T00:00:00Z')
    usage(db, 'child-usage', 'step-a')
    run = read_snapshot(db, now=datetime(2026, 10, 19, tzinfo=timezone.utc)).workflows[0].runs[0]
    kwargs = dict(workflow='sdlc-process', user_id='steve', scope=MeasurementScope('all', 'root'),
                  start='2026-10-01', end='2026-10-10')
    assert run.run_id == 'phase' and run.version == 'v1'
    assert read_builtin(db, builtin='runs', **kwargs).versions['v1'].value == 1
    assert read_builtin(db, builtin='corrections', **kwargs).versions['v1'].value == sum(f.is_correction for f in run.corrections) == 2
    assert read_builtin(db, builtin='fresh_tokens', **kwargs).versions['v1'].value == 1_000_000
    assert read_builtin(db, builtin='cost_usd', **kwargs).versions['v1'].value > 0


def test_verdict_carries_synced_version_attribute(db):
    node(db, 'old')
    db.execute("INSERT INTO measure_definitions(owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,version_attr) VALUES ('owner','event','demo.run',1,?,'/workflows/sdlc-process/measures.yaml','hash','2026-01-01','revision')", (json.dumps({'attributes': {}}),))
    history = read_snapshot(db).workflows[0]
    assert history.version_attr == 'revision'


def test_version_attribute_resync_updates_all_rows_and_both_reads(db, monkeypatch):
    from pathlib import Path
    from core import agent_kpi_read
    from infra.measure_registry import WorkflowOwner

    node(db, 'old')
    path = '/workflows/sdlc-process/measures.yaml'
    owners = {'sdlc-process': WorkflowOwner(Path('/workflows/sdlc-process'), 'owner')}
    monkeypatch.setattr(agent_kpi_read, 'thread_workflow_owners', lambda _owner, _db: owners)
    text = '''schema: corsia.measures/1
namespace: demo
workflow: sdlc-process
version_attr: revision
events:
  demo.done:
    attributes: {revision: id, edition: id}
    dedupe: [revision]
  demo.extra:
    attributes: {revision: id, edition: id}
    dedupe: [revision]
'''
    sync(db, owner_thread_id='owner', path=path, text=text, workflows=owners)
    changed = text.replace('dedupe: [revision]', 'dedupe: [edition]', 1).split('  demo.extra:')[0]
    sync(db, owner_thread_id='owner', path=path, text=changed, workflows=owners)
    before = db.execute('SELECT kind,name,version,spec_json,file_sha256 FROM measure_definitions ORDER BY version').fetchall()
    sync(db, owner_thread_id='owner', path=path,
         text=changed.replace('version_attr: revision', 'version_attr: edition'), workflows=owners)
    after = db.execute('SELECT kind,name,version,spec_json,file_sha256 FROM measure_definitions ORDER BY version').fetchall()
    assert [tuple(row) for row in before] == [tuple(row) for row in after]
    assert len(after) == 4
    assert all(row[0] == 'edition' for row in db.execute('SELECT version_attr FROM measure_definitions'))
    assert agent_kpi_read.definitions(db, 'owner')[0]['version_attr'] == 'edition'
    assert read_snapshot(db).workflows[0].version_attr == 'edition'


@pytest.mark.parametrize('formula,num', [('count', '{builtin: runs}'), ('rate_per_run', '{event: qa.done}')])
def test_agent_file_refuses_workflow_builtins(formula, num):
    text = small(f"rate:\n  title: Rate\n  unit: count\n  better: higher\n  formula: {formula}\n  num: {num}\n  min_n: 1", workflow=False, corroborate=False)
    with pytest.raises(ValueError, match='builtin|rate_per_run'):
        sync(registry_db(), owner_thread_id='agent-qg', path=own('agent-qg'), text=text)
