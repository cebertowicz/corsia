"""Create versioned workflow runs through the plan and hold APIs in e2e-local's disposable state.

Every run, step, gate answer and correction here is written by the same writers the product uses:
``tools.plans.PlanUpdateTool`` for the plan and run shape (the run's version comes from the committed
workflow definition in the disposable workspace, exactly as a real run records it) and ``plans.holds``
for a person's answer at an acceptance gate. Nothing is INSERTed into a plan table.

Test arrangements this file makes, all of them disclosed to the caller in its JSON result:
- ``CLOCK_PINS``: the plan and usage writers' clocks are pinned per run, so synthetic events and
  calls fall on a past date. No product API backdates a node; these are seed clock overrides.
- ``TURNS``: helper identities admit plan writes, then disposable Turn lifecycle records provide
  working intervals. Usage is recorded with the plan binding returned at call time.
- ``GATE``: the hold writer settles each acceptance gate.
- ``CORRECTIONS``: synthetic person messages are recorded through the classification writer.
These synthetic facts and the pinned plan clock are disclosed in the JSON result.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from infra.database import get_db
from core.corrections_classifier import Classification
from core.corrections_recording import ACTIVE_VERSION_SETTING, ClassifierVersion, version_key
from core.turn_core import TurnIdentity
from infra.input_admission import accept_thread_input
from infra.input_writes import begin_batch_write, settle_batch_write
from infra import llm_usage
from infra.llm_usage import record_llm_usage
from infra.measure_registry import sync as sync_measures
from infra.settings import set_setting
from infra.thread_driver import finish_turn as settle_turn
from infra.thread_inputs import start_idle_turn
from jobs.corrections_recording import candidate_for_message, record_classification
from plans import holds, service
from plans.usage_calls import call_binding
from tests.plan_execution_helpers import execute_as, finish_turn, open_turn, registry_of
from tools.plans import PlanUpdateTool
from tools.workflow_owners import thread_workflow_owners

#: One day offset per run, negative = days before today. All v1 offsets precede every v2 offset, so
#: v2's first use is genuinely later than the whole v1 population the comparison engine counts.
CLOSE_V1_DAYS = [-84, -78, -73, -68, -63, -58, -54, -50, -46, -42, -38, -34, -30,
                 -27, -25, -23, -21, -19, -17, -15, -13, -11]
CLOSE_V2_DAYS = [-10, -9, -8, -7, -6, -5, -4, -3, -2]
INVOICE_DAYS = [-25, -18, -5, -2]
#: Both older than the four completed weeks any window can select, so this workflow lists with no runs.
ARCHIVE_DAYS = [-120, -105]
CLOSE = 'e2e-close'
INVOICE = 'e2e-invoice'
ARCHIVE = 'e2e-archive'


def git(workspace: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=workspace, check=True, stdout=subprocess.DEVNULL)


def commit(workspace: Path, message: str) -> None:
    git(workspace, 'add', 'skills')
    git(workspace, '-c', 'user.name=E2E', '-c', 'user.email=e2e@example.invalid', 'commit', '-qm', message)


def skill(workspace: Path, ref: str, steps: list[tuple[str, str, bool]], owner: str) -> Path:
    """Write one workflow definition folder; ``steps`` is (id, title, is_acceptance_gate)."""
    folder = workspace / 'skills' / ref
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(
        f"---\nname: {ref}\ndescription: Disposable e2e workflow {ref}\n"
        "metadata:\n  corsia-data: corsia.yaml\n  corsia-format: '1'\n---\nRun the disposable e2e flow.\n",
        encoding='utf-8')
    definition = [{'id': step_id, 'title': title, 'required': True, 'check': f'{title} done',
                   'difficulty': {'default': 'routine'}} for step_id, title, gate in steps]
    for item, (_, _, gate) in zip(definition, steps):
        if gate:
            item['gate'] = 'acceptance'
    sidecar = folder / 'corsia.yaml'
    sidecar.write_text(f'handler: {{kind: instruction}}\nworkflow:\n  owner: {owner}\n  steps:\n'
                       + ''.join(f'    - {json.dumps(item)}\n' for item in definition), encoding='utf-8')
    namespace = ref.replace('-', '_')
    (folder / 'measures.yaml').write_text(
        f'schema: corsia.measures/1\nnamespace: {namespace}\nworkflow: {ref}\nevents:\n'
        f'  {namespace}.done:\n    attributes: {{result: {{enum: [ok, missed]}}}}\n'
        '    dedupe: [run_id]\nkpis:\n  completion:\n    title: Completed\n    unit: percent\n'
        f'    better: higher\n    formula: share\n    num: {{event: {namespace}.done, attr: result, values: [ok]}}\n'
        '    min_n: 20\n', encoding='utf-8')
    return sidecar


class Seed:
    """One thread's plan writes, driven as that thread through the real plan tool."""

    def __init__(self, registry, thread_id: str, plan_id: str) -> None:
        self.registry, self.thread_id, self.plan_id = registry, thread_id, plan_id
        self.serial = 0
        self.gate_writer = 'hold'
        self.turn_number = 0
        self.identity = None

    async def update(self, operation: str, params: dict) -> dict:
        self.serial += 1
        result = await execute_as(self.registry, self.identity, self.thread_id, 'plan_update',
                                  {'plan_id': self.plan_id, 'mutation_id': f'{self.thread_id}-{self.serial}',
                                   'operation': operation, 'params': params})
        if not result.startswith('{'):
            raise RuntimeError(f'{operation}: {result[:400]}')
        return json.loads(result)

    def revision(self, node: str) -> int:
        db = get_db()
        try:
            return int(service._node_row(db, node)['revision'])
        finally:
            db.close()

    def at(self, stamp: str) -> None:
        service._now = lambda: stamp  # the seed's pinned writer clocks (disclosed)
        llm_usage.utc_now_iso = lambda: stamp

    async def run(self, ref: str, days: int, mode: str, *, correct: bool, gate_index: int | None) -> dict:
        """One run of ``ref`` on ``days`` offset: clean, sent back at the gate, or failed on a step.

        ``gate_index`` names which step of this workflow is its acceptance gate, as the seed's own
        definition says; None for a workflow with no gate.
        """
        stamp = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
            hour=9, minute=0, second=0, microsecond=0).isoformat()
        self.at(stamp)
        self.turn_number += 1
        helper_id = f'{self.thread_id}-helper-{self.turn_number}'
        db = get_db()
        try:
            self.identity = open_turn(db, self.thread_id, helper_id)
        finally:
            db.close()
        created = await self.update('add', {'kind': 'phase', 'title': f'Run {ref}', 'acceptance_criteria':
                                            'Every required step settled', 'workflow_ref': ref, 'size': 'M'})
        phase = created['node']['id']
        first = created['steps'][0]['id']
        gate = None if gate_index is None else created['steps'][gate_index]['id']
        await self.update('start', {'node_id': first, 'expected_revision': self.revision(first)})
        db = get_db()
        try:
            finish_turn(db, helper_id)
            accept_thread_input(self.thread_id, role='user', content='Run the workflow step.',
                                intent='apply_now', message_id=f'{phase}-input', created_at=stamp, db=db)
            turn_id = f'{phase}-turn'
            batch = start_idle_turn(self.thread_id, turn_id=turn_id, boot_epoch='e2e', started_at=stamp, db=db)
            if batch is None or not hasattr(batch, 'turn_order'):
                active = db.execute("SELECT id,status,boot_epoch FROM active_turns WHERE thread_id=? AND status='running'",
                                    (self.thread_id,)).fetchall()
                pending = db.execute("SELECT id,delivery_state,active_turn_id FROM messages WHERE thread_id=? "
                                     "AND delivery_state='queued' ORDER BY input_order", (self.thread_id,)).fetchall()
                raise RuntimeError(f'Could not start real turn for {phase}: {batch}; '
                                   f'active={[tuple(row) for row in active]}; queued={[tuple(row) for row in pending]}')
            self.identity = TurnIdentity(turn_id, batch.turn_order)
            attempt_id = f'{phase}-mock-provider-write'
            begin_batch_write(batch, attempt_id=attempt_id, started_at=stamp, db=db)
            settle_batch_write(attempt_id, outcome='accepted', db=db)
        finally:
            db.close()
        binding = call_binding(self.thread_id)
        if not binding or binding.get('plan_node_id') != first:
            raise RuntimeError(f'No execution binding for {first}: {binding}')
        record_llm_usage(self.thread_id, 'claude-code/claude-opus-5-5',
                         {'prompt_tokens': 2000, 'completion_tokens': 500, 'cache_creation_tokens': 0,
                          'cache_read_tokens': 0, 'total_tokens': 2500, 'cost_usd': 0.04},
                         source='e2e', turn_id=turn_id,
                         usage_key=f'{phase}-usage', binding=binding)
        if mode == 'failed':
            await self.update('fail', {'node_id': first, 'expected_revision': self.revision(first),
                                       'evidence': 'The statements were missing'})
            await self.finish_run(turn_id, stamp, phase, correct)
            return {'phase': phase, 'ref': ref, 'day': days, 'mode': mode, 'gate': 'none'}
        await self.update('mark_done', {'node_id': first, 'expected_revision': self.revision(first),
                                        'evidence': 'Statements gathered', 'verification_result': 'passed'})
        rounds = 1
        if gate is not None:
            rounds = await self.settle_gate(gate, mode)
        await self.finish_run(turn_id, stamp, phase, correct)
        return {'phase': phase, 'ref': ref, 'day': days, 'mode': mode, 'gate': self.gate_writer,
                'rounds': rounds}

    async def finish_run(self, turn_id: str, stamp: str, phase: str, correct: bool) -> None:
        ended = (datetime.fromisoformat(stamp) + timedelta(minutes=25)).isoformat()
        result = settle_turn(turn_id, outcome='completed', ended_at=ended,
                             successor_turn_id=f'{phase}-successor', boot_epoch='e2e',
                             mark_idle_checkpoint=False)
        if result.successor is not None:
            raise RuntimeError(f'Unexpected successor for {phase}: {result.successor}')
        if correct:
            at = (datetime.fromisoformat(stamp) + timedelta(minutes=20)).isoformat()
            message_id = f'{phase}-correction'
            accept_thread_input(self.thread_id, role='user', content='Please correct the figures in this result.',
                                intent='apply_now', message_id=message_id, created_at=at)
            candidate = candidate_for_message(message_id)
            if candidate is None:
                raise RuntimeError(f'Correction candidate missing: {message_id}')
            version = ClassifierVersion('e2e-asset', 'e2e-counter', 'e2e-head', 0.5, 'e2e-policy')
            set_setting(ACTIVE_VERSION_SETTING, {'key': version_key(version)})
            if not await record_classification(candidate, Classification(version, 0.99, 'correction', False, 8)):
                raise RuntimeError(f'Correction not recorded: {message_id}')

    async def settle_gate(self, gate: str, mode: str) -> int:
        """Answer the acceptance gate once (approved) or twice (sent back, then approved)."""
        verdicts = ['changes_requested', 'approved'] if mode == 'sent_back' else ['approved']
        for verdict in verdicts:
            await self._answer(gate, verdict)
        await self.update('mark_done', {'node_id': gate, 'expected_revision': self.revision(gate),
                                        'evidence': 'Reviewed and accepted', 'verification_result': 'passed'})
        return len(verdicts)

    async def _answer(self, gate: str, verdict: str) -> None:
        """The person's verdict, by the hold route the gate uses when the answer arrives as a hold."""
        db = get_db()
        try:
            revision = int(service._node_row(db, gate)['revision'])
            holds.place_hold(gate, kind='blocked', reason_key='needs_review', reason='Review',
                             owner_kind='user', owner_id=None, expected_revision=revision,
                             actor_id=None, provenance='e2e.ask', db=db)
            hold = holds.active_holds(gate, db=db)[-1]['id']
            holds.clear_write(gate, hold, expected_kind='blocked', actor_id=None,
                              provenance='user.ask_cleared', detail={'verdict': verdict}, db=db)
            db.commit()
        finally:
            db.close()


def _is_gate(created: dict, step_id: str) -> bool:
    """Whether the created step carries the acceptance gate (the tool names it in the step payload)."""
    step = next(item for item in created['steps'] if item['id'] == step_id)
    return bool(step.get('gate')) or step.get('gate_mode') is not None


async def main(threads: list[str]) -> None:
    workspace = Path(os.environ['CORSIA_E2E_WORKSPACE']).resolve()
    state = Path(os.environ['CORSIA_E2E_STATE_DIR']).resolve()
    assert os.environ.get('CORSIA_E2E_MOCK') == '1' and os.environ.get('CORSIA_TEST_MODE') == '1'
    assert workspace == Path(os.environ['CORSIA_WORKSPACE']).resolve()
    assert state == Path(os.environ['CORSIA_STATE_DIR']).resolve() and (state / 'corsia.db').is_file()
    only_one = os.environ.get('CORSIA_E2E_WORKFLOW_ONE') == '1'

    sidecar = skill(workspace, CLOSE, [('gather', 'Gather statements', False),
                                       ('review', 'Review accounts', True)], threads[0])
    if not only_one:
        skill(workspace, INVOICE, [('draft', 'Draft the invoice', False), ('send', 'Approve and send', True)], threads[1])
        skill(workspace, ARCHIVE, [('file', 'File the closed period', False)], threads[0])
    git(workspace, 'init', '-q')
    commit(workspace, 'Workflow versions one')
    owned = [(CLOSE, threads[0])]
    if not only_one:
        owned += [(INVOICE, threads[1]), (ARCHIVE, threads[0])]
    for ref, owner in owned:
        path = workspace / 'skills' / ref / 'measures.yaml'
        db = get_db()
        try:
            sync_measures(db, owner_thread_id=owner, path=str(path), text=path.read_text(encoding='utf-8'),
                          workflows=thread_workflow_owners(owner, db))
        finally:
            db.close()

    seeds: list[Seed] = []
    for thread_id in threads[:2]:
        db = get_db()
        try:
            plan = service.ensure_plan('thread', thread_id, db=db)
            accept_thread_input(thread_id, role='user', content='Run the disposable e2e flows.',
                                intent='apply_now', message_id=f'{thread_id}-workflow-live-authority', db=db)
        finally:
            db.close()
        seed = Seed(registry_of(PlanUpdateTool()), thread_id, plan.id)
        seeds.append(seed)

    runs: list[dict] = []
    v1_days = CLOSE_V1_DAYS
    for index, day in enumerate(v1_days):
        mode = ['clean', 'sent_back', 'failed'][index % 3]
        runs.append(await seeds[index % len(seeds)].run(
            CLOSE, day, mode, correct=index % 6 == 0, gate_index=1))
    sidecar.write_text(sidecar.read_text(encoding='utf-8') + '# version two: a stricter check\n',
                       encoding='utf-8')
    commit(workspace, 'Workflow version two')
    for index, day in enumerate(CLOSE_V2_DAYS):
        mode = ['clean', 'sent_back', 'failed'][index % 3]
        runs.append(await seeds[index % len(seeds)].run(
            CLOSE, day, mode, correct=index % 6 == 0, gate_index=1))
    if not only_one:
        for index, day in enumerate(INVOICE_DAYS):
            runs.append(await seeds[index % len(seeds)].run(
                INVOICE, day, ['clean', 'failed', 'clean', 'sent_back'][index], correct=False, gate_index=1))
        for index, day in enumerate(ARCHIVE_DAYS):
            runs.append(await seeds[index % len(seeds)].run(ARCHIVE, day, 'clean', correct=False, gate_index=None))

    # Seed delegated work after the parent's runs release its executor slot.
    if len(threads) > 2 and not only_one:
        from plans.delegation import attach_spawned_thread

        db = get_db()
        try:
            node = service.add_node(seeds[0].plan_id, kind='task', title='Delegated workflow',
                                    acceptance_criteria='Run the workflow', db=db)
            service.transition_node(node.id, 'active', db=db)
            worker_plan = service.ensure_plan('thread', threads[2], db=db)
            attach_spawned_thread(node.id, threads[2], actor_id=threads[0], db=db)
            accept_thread_input(threads[2], role='user', content='Run the disposable workflow.',
                                intent='apply_now', message_id=f'{threads[2]}-workflow-live-authority', db=db)
            db.commit()
        finally:
            db.close()
        worker = Seed(registry_of(PlanUpdateTool()), threads[2], worker_plan.id)
        runs.append(await worker.run(CLOSE, -6, 'failed', correct=False, gate_index=1))

    print(json.dumps({
        'runs': [run['phase'] for run in runs],
        'threads': threads,
        'detail': runs,
        'arrangements': {
            'clock_pins': len(runs),
            'pinned_writers': ['plans.service._now', 'infra.llm_usage.utc_now_iso'],
            'turns': 'one helper and one disposable lifecycle Turn per run',
            'gate_writers': sorted({run['gate'] for run in runs}),
            'versions': 'git commit of the disposable workspace skill folder',
            'usage': 'synthetic priced calls through the usage writer',
            'corrections': 'synthetic person messages through the classification writer',
            'owners': 'workflow sidecars and measures declarations synced by their synthetic owners',
        },
        'limitations': [
            'Plan and usage writer clocks are pinned to synthetic run dates.',
            'Workflow phase settlement and delegated usage attribution require verification before capture.',
        ],
    }))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
