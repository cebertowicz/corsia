"""Create versioned workflow runs through the plan tool in e2e-local's disposable state."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from infra.database import get_db
from infra.input_admission import accept_thread_input
from plans import holds, service
from tests.plan_execution_helpers import execute_as, finish_turn, open_turn, registry_of
from tools.plans import PlanUpdateTool


def git(workspace: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=workspace, check=True, stdout=subprocess.DEVNULL)


async def main(thread_id: str) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    # Create append-only events in the completed window using the plan writer's clock.
    service._now = lambda: stamp
    workspace = Path(os.environ['CORSIA_E2E_WORKSPACE']).resolve()
    state = Path(os.environ['CORSIA_E2E_STATE_DIR']).resolve()
    assert os.environ.get('CORSIA_E2E_MOCK') == '1' and os.environ.get('CORSIA_TEST_MODE') == '1'
    assert workspace == Path(os.environ['CORSIA_WORKSPACE']).resolve()
    assert state == Path(os.environ['CORSIA_STATE_DIR']).resolve() and (state / 'corsia.db').is_file()
    skill = workspace / 'skills' / 'e2e-close'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text("---\nname: e2e-close\ndescription: Close a period\nmetadata:\n  corsia-data: corsia.yaml\n  corsia-format: '1'\n---\nClose the period.\n", encoding='utf-8')
    sidecar = skill / 'corsia.yaml'
    sidecar.write_text("handler: {kind: instruction}\nworkflow:\n  steps:\n    - {id: gather, title: Gather statements, required: true, check: Gathered, difficulty: {default: routine}}\n    - {id: review, title: Review accounts, required: true, gate: acceptance, check: Reviewed, difficulty: {default: routine}}\n", encoding='utf-8')
    git(workspace, 'init', '-q')
    git(workspace, 'add', 'skills/e2e-close')
    git(workspace, '-c', 'user.name=E2E', '-c', 'user.email=e2e@example.invalid', 'commit', '-qm', 'Workflow version one')
    db = get_db()
    try:
        plan = service.ensure_plan('thread', thread_id, db=db)
        accept_thread_input(thread_id, role='user', content='Close the period.', intent='apply_now',
                            message_id='workflow-live-authority', db=db)
        identity = open_turn(db, thread_id, 'workflow-live-turn')
    finally:
        db.close()
    registry = registry_of(PlanUpdateTool())
    serial = 0

    async def update(operation: str, params: dict):
        nonlocal serial
        serial += 1
        result = await execute_as(registry, identity, thread_id, 'plan_update',
            {'plan_id': plan.id, 'mutation_id': f'workflow-live-{serial}', 'operation': operation, 'params': params})
        if not result.startswith('{'):
            raise RuntimeError(result)
        return json.loads(result)

    def revision(node: str) -> int:
        connection = get_db()
        try:
            return int(service._node_row(connection, node)['revision'])
        finally:
            connection.close()

    async def create_run(failed: bool):
        created = await update('add', {'kind': 'phase', 'title': 'Run e2e close', 'acceptance_criteria': 'Period closed',
                                       'workflow_ref': 'e2e-close', 'size': 'M'})
        phase = created['node']['id']
        first, gate = [step['id'] for step in created['steps']]
        await update('start', {'node_id': first, 'expected_revision': revision(first)})
        if failed:
            await update('fail', {'node_id': first, 'expected_revision': revision(first), 'evidence': 'Statements missing'})
        else:
            await update('mark_done', {'node_id': first, 'expected_revision': revision(first), 'evidence': 'Statements gathered', 'verification_result': 'passed'})
            await update('start', {'node_id': gate, 'expected_revision': revision(gate)})
            db = get_db()
            try:
                for verdict in ('changes_requested', 'approved'):
                    holds.place_hold(gate, kind='blocked', reason_key='needs_review', reason='Review', owner_kind='user', owner_id=None,
                                     expected_revision=int(service._node_row(db, gate)['revision']), actor_id=None, provenance='e2e.ask', db=db)
                    hold = holds.active_holds(gate, db=db)[-1]['id']
                    holds.clear_write(gate, hold, expected_kind='blocked', actor_id=None, provenance='user.ask_cleared',
                                      detail={'verdict': verdict}, db=db)
                    db.commit()
            finally:
                db.close()
            await update('mark_done', {'node_id': gate, 'expected_revision': revision(gate), 'evidence': 'Reviewed', 'verification_result': 'passed'})
        return phase

    first = await create_run(False)
    runs = [first]
    if os.environ.get('CORSIA_E2E_WORKFLOW_ONE') != '1':
        service._now = lambda: (datetime.fromisoformat(stamp) + timedelta(hours=1)).isoformat()
        sidecar.write_text(sidecar.read_text(encoding='utf-8') + '# version two\n', encoding='utf-8')
        git(workspace, 'add', 'skills/e2e-close/corsia.yaml')
        git(workspace, '-c', 'user.name=E2E', '-c', 'user.email=e2e@example.invalid', 'commit', '-qm', 'Workflow version two')
        runs.append(await create_run(True))
    db = get_db()
    try:
        finish_turn(db, 'workflow-live-turn')
        db.commit()
    finally:
        db.close()
    print(json.dumps({'runs': runs, 'thread': thread_id}))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
