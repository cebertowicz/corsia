import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OperatorArtifactLink, OperatorNode } from '$lib/api/plans';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import { doFirstOf, doStep, doTarget, groupLabel, stepState } from './personStepChip';

const calls = vi.hoisted(() => ({
	focus: vi.fn(async () => {}),
	ask: vi.fn(),
	refresh: vi.fn(async () => {}),
	payload: null as unknown,
}));
vi.mock('$lib/planNodeFocus', () => ({ focusPlanNode: calls.focus }));
vi.mock('$lib/planLeafReturn', () => ({ openPlanAsk: calls.ask }));
vi.mock('$lib/stores/plans.svelte', () => ({
	peekEntry: () => ({ payload: calls.payload }),
	refresh: calls.refresh,
}));

const doc: OperatorArtifactLink = {
	id: 'artifact-1', role: 'input', integration: 'corsia', type: 'document', external_id: 'doc-1',
	title: 'Run the proof', path: 'shared/run-the-proof.md', state: 'active', attention_state: null,
};
const step = (over: Partial<OperatorNode> = {}) =>
	operatorNode({ id: 'step-1', title: 'Run the proof', assignee: 'user', ...over } as never);
const read = (...nodes: OperatorNode[]) => ({ plans: [{ nodes }] });
const card = { thread_id: 't1', node_id: 'step-1', title: 'Run the proof' };

beforeEach(() => {
	calls.focus.mockClear();
	calls.ask.mockClear();
	calls.refresh.mockClear();
	calls.payload = null;
});

describe('stepState', () => {
	it('takes the plan node over the card, so every surface settles the chip', () => {
		expect(stepState({ step_state: 'open' }, step({ state: 'accepted' }))).toBe('done');
		expect(stepState({ step_state: 'open' }, step({ state: 'accepted_with_followup' }))).toBe('done');
		expect(stepState({ step_state: 'open' }, step({ state: 'closed' }))).toBe('closed');
		expect(stepState({ step_state: 'done' }, step({ state: 'pending' }))).toBe('open');
	});

	it('falls back to the card for a step the plan read no longer lists, and to open without either', () => {
		expect(stepState({ step_state: 'closed' }, undefined)).toBe('closed');
		expect(stepState({}, undefined)).toBe('open');
	});
});

describe('doTarget', () => {
	it('opens the instructions document when the step links one as input', () => {
		expect(doTarget(step({ artifacts: [doc] }))).toEqual({ kind: 'document', artifact: doc });
	});

	it('opens the step’s details in the plan without one, or when the link is an ask', () => {
		expect(doTarget(step())).toEqual({ kind: 'details' });
		expect(doTarget(step({ artifacts: [{ ...doc, role: 'attention' }] }))).toEqual({ kind: 'details' });
		expect(doTarget(undefined)).toEqual({ kind: 'details' });
	});
});

describe('doStep', () => {
	it('opens the document through the plan row’s own open path', async () => {
		calls.payload = read(step({ artifacts: [doc] }));
		await doStep(card);
		expect(calls.ask).toHaveBeenCalledWith('t1', doc, 'Run the proof', expect.any(Function));
		expect(calls.focus).not.toHaveBeenCalled();
		expect(calls.refresh).not.toHaveBeenCalled();
	});

	it('focuses the step in the plan when it has no document', async () => {
		calls.payload = read(step());
		await doStep(card);
		expect(calls.focus).toHaveBeenCalledWith('t1', 'step-1');
		expect(calls.ask).not.toHaveBeenCalled();
	});

	it('reads the plan first when the node is not loaded yet, then opens what that read says', async () => {
		calls.refresh.mockImplementationOnce(async () => { calls.payload = read(step({ artifacts: [doc] })); });
		await doStep(card);
		expect(calls.refresh).toHaveBeenCalledWith({ kind: 'thread', id: 't1' });
		expect(calls.ask).toHaveBeenCalledOnce();
	});

	it('does nothing for a card with no node', async () => {
		await doStep({ thread_id: 't1', node_id: null, title: 'x' });
		expect(calls.focus).not.toHaveBeenCalled();
		expect(calls.ask).not.toHaveBeenCalled();
	});
});

describe('group', () => {
	it('names the count and opens the plan at the first step still waiting', async () => {
		expect(groupLabel(7)).toBe('7 steps');
		await doFirstOf([{ thread_id: 't1', node_id: 'a' }, { thread_id: 't1', node_id: 'b' }]);
		expect(calls.focus).toHaveBeenCalledWith('t1', 'a');
	});
});
