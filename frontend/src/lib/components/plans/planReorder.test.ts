import { describe, expect, it } from 'vitest';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import { positionBeside } from './planReorder';
import { inPlanOrder, planOrderGroup } from './planTreeFold';

describe('position among all open siblings', () => {
	const siblings = ['ready-a', 'wait-a', 'wait-b', 'ready-b', 'wait-c', 'ready-c']
		.map((id) => operatorNode({ id }));
	it('places ready rows around interleaved waits without mistaking group positions for sibling positions', () => {
		expect(positionBeside(siblings, 'ready-c', 'ready-b', true)).toBe(3);
		expect(positionBeside(siblings, 'ready-c', 'ready-a', true)).toBe(0);
		expect(positionBeside(siblings, 'ready-a', 'ready-b', false)).toBe(3);
		expect(positionBeside(siblings, 'ready-a', 'ready-c', true)).toBe(4);
		expect(positionBeside(siblings, 'ready-a', 'ready-c', false)).toBe(5);
	});
	it('rejects missing or identical rows', () => {
		expect(positionBeside(siblings, 'missing', 'ready-a', true)).toBeNull();
		expect(positionBeside(siblings, 'ready-a', 'ready-a', false)).toBeNull();
	});
});

describe('the rows a drag or Move reorders', () => {
	// The sheet shows every open row outside Later in plan order (arrangeItems), and the
	// server offers Move on every open row among its open siblings (plans.user_steering).
	// So waits, timed or not, move like any other open row; Later and finished rows do not.
	it('takes every open row outside Later, waits included, and leaves Later and finished rows out', () => {
		const ready = operatorNode();
		const untimed = operatorNode({ display: { label_key: 'waiting', tone_key: 'waiting' }, execution: { ...ready.execution, condition: 'waiting' } });
		const cause = { kind: 'waiting' as const, reason_key: 'watch', reason: 'Waiting', owner_kind: 'thread' as const, owner_id: 'agent', ends_at: '2030-01-01T00:00:00Z' };
		const timed = operatorNode({ ...untimed, execution: { ...untimed.execution, causes: [cause] } });
		const later = operatorNode({ ...ready, later: true });
		const finished = operatorNode({ state: 'accepted', display: { label_key: 'finished', tone_key: 'complete' } });
		const now = Date.parse('2026-01-01T00:00:00Z');
		expect([ready, untimed, timed, later, finished].map((node) => inPlanOrder(node, now, new Set()))).toEqual([true, true, true, false, false]);
	});
	it('keeps a finished parent that still holds open work in the list', () => {
		const parent = operatorNode({ id: 'parent', state: 'accepted', display: { label_key: 'finished', tone_key: 'complete' } });
		expect(inPlanOrder(parent, 0, new Set(['parent']))).toBe(true);
	});
	it('puts every unfolded step of a run in one sequence, since the run shows in plan order', () => {
		const ready = operatorNode();
		const cause = { kind: 'waiting' as const, reason_key: 'watch', reason: 'Waiting', owner_kind: 'thread' as const, owner_id: 'agent', ends_at: '2030-01-01T00:00:00Z' };
		const timed = operatorNode({ display: { label_key: 'waiting', tone_key: 'waiting' }, execution: { ...ready.execution, condition: 'waiting', causes: [cause] } });
		const scheduled = operatorNode({ ...timed, execution: { ...timed.execution, causes: [{ ...cause, scheduled_reason: 'Later' }] } });
		const finished = operatorNode({ state: 'accepted' });
		const folded = operatorNode({ state: 'accepted', folded: true });
		const now = Date.parse('2026-01-01T00:00:00Z');
		expect([ready, timed, scheduled, finished, folded].map((node) => planOrderGroup(node, now, new Set(), true))).toEqual(['run', 'run', 'run', 'run', null]);
	});
});
