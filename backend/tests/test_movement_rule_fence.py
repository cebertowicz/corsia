"""Movement report paths stay within the report's project across rule actions."""

import json

import pytest

from tests.rule_worker_helpers import (  # noqa: F401
    OWNER, atool, owner_script, run_scripts, starts, world,
)


def report_with_other_project(world):
    from infra.conductor import set_project_conductor
    from infra.database import get_db
    from infra import config

    db = get_db()
    name = world['project'] + '-other'
    other = config.WORKSPACE / 'projects' / name
    other.mkdir(parents=True)
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,'t','t')", (name, name))
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES (?,1)", (name,))
    db.commit()
    assert set_project_conductor(world['project'], OWNER, db=db)
    private = str(other / 'private.md')
    own = str(world['root'] / 'own.md')
    report = {'scope': world['project'], 'report_date': '2026-10-15', 'breach': False,
              'moves': [{'series_id': 's', 'owner_agent': OWNER, 'value': 3, 'normal_range': [0, 1],
                         'preceding_changes': [
                             {'kind': 'skill', 'ref': private, 'at': '2026-10-14T08:00:00+00:00'},
                             {'kind': 'skill', 'ref': own, 'at': '2026-10-14T08:00:00+00:00'}]}]}
    return db, report, private, own


async def create_rule(action, **kwargs):
    rule = await atool(OWNER, operation='create', connector='measurement', event_type='measurement.movement_digest',
                       action=action, match=[{'field': 'scope', 'op': 'present'}], **kwargs)
    assert isinstance(rule, dict), rule
    return rule['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['note', 'script'])
async def test_project_note_and_script_receive_filtered_report(world, monkeypatch, surface):
    from events.movement_report import deliver_movement_report_sync

    db, report, private, own = report_with_other_project(world)
    script = owner_script(world, monkeypatch, 'movement.sh')
    await create_rule('run_script', script_id=script)
    deliver_movement_report_sync(report, db=db)
    await run_scripts()
    row = db.execute("SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", (OWNER,)).fetchone()
    [event] = world['script_inputs']
    text = row['summary'] if surface == 'note' else json.dumps(event)
    assert private not in text
    assert own in text


@pytest.mark.asyncio
async def test_project_worker_brief_receives_filtered_report(world, monkeypatch):
    from automation import worker_runner
    from events.movement_report import deliver_movement_report_sync

    db, report, private, own = report_with_other_project(world)
    rule = await create_rule('start_worker', workflow_id='worker-flow', request_identity_fact='report_key')
    deliver_movement_report_sync(report, db=db)
    assert starts(rule)[0][0] == 'queued'
    captured = []
    original = worker_runner.inputs.brief

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(worker_runner.inputs, 'brief', capture)
    world['reply'].write_text('HANG')
    assert await worker_runner.drive() == 1
    [brief] = captured
    assert private not in brief
    assert own in brief


@pytest.mark.asyncio
async def test_workspace_writer_cannot_author_rules(world):
    from infra.database import get_db
    db = get_db()
    db.execute("UPDATE threads SET project_id=NULL WHERE id=?", (OWNER,))
    db.commit()
    result = await atool(OWNER, operation='create', connector='measurement', event_type='measurement.movement_digest',
                         action='deliver', match=[{'field': 'scope', 'op': 'present'}])
    assert isinstance(result, str)
    assert 'Rules cover a project' in result
