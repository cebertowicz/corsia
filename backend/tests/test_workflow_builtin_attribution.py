"""Plan-sourced correction facts retain the corrected task identity."""
import json

from core.correction_attribution import plan_attributed_facts
from tests.test_workflow_verdict_read import db, node


def test_plan_corrections_and_missing_sources(db):
    for name in ('rejected', 'source', 'correction'):
        node(db, name)
    node(db, 'other-workflow', workflow='other')
    db.executemany('INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at) '
                   'VALUES (?,?,\'test\',?,?,\'2026-01-01\')', [
        ('r', 'rejected', 'review_rejected', '{}'),
        ('c', 'correction', 'correction_created', json.dumps({'source_node_id': 'source'})),
    ])
    for kind, subject in [('sent_back', 'plan-review-rejected:r'),
                          ('self_caught', 'plan-correction-created:c'),
                          ('self_caught', 'plan-correction-created:missing'),
                          ('sent_back', 'plan-review-rejected:missing')]:
        db.execute('INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,'
                   'subject_key,attributes) VALUES (?,?,\'measurement.correction\',\'corsia\',\'observed\', '
                   "'2026-01-01','2026-01-01',?,?)",
                   (subject, subject, subject, json.dumps({'source_kind': kind, 'label': 'correction'})))
    facts = plan_attributed_facts(db)
    assert [f.node_id for f in facts] == ['rejected', 'source', None, None]
    assert facts[2].source_at is None and facts[3].source_at is None

    from core.measurement_scope import MeasurementScope
    from core.workflow_builtin_operands import read_builtin
    result = read_builtin(db, builtin='corrections', workflow='sdlc-process', user_id='steve',
                          scope=MeasurementScope('all', 'root'), start='2026-01-01', end='2026-01-02')
    assert result.versions['v1'].value == 2
    other = read_builtin(db, builtin='corrections', workflow='other', user_id='steve',
                         scope=MeasurementScope('all', 'root'), start='2026-01-01', end='2026-01-02')
    assert other.versions['v1'].value == 0
