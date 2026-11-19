import { describe, expect, it } from 'vitest';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import { positionBeside } from './planReorder';
import { planOrderGroup } from './planTreeFold';

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

describe('the renderer groups eligible for reorder', () => {
	it('allows ready work and untimed waits but excludes timed, scheduled and finished rows', () => {
		const ready = operatorNode();
		const untimed = operatorNode({ display: { label_key: 'waiting', tone_key: 'waiting' }, execution: { ...ready.execution, condition: 'waiting' } });
		const cause = { kind: 'waiting' as const, reason_key: 'watch', reason: 'Waiting', owner_kind: 'thread' as const, owner_id: 'agent', ends_at: '2030-01-01T00:00:00Z' };
		const timed = operatorNode({ ...untimed, execution: { ...untimed.execution, causes: [cause] } });
		const scheduled = operatorNode({ ...timed, execution: { ...timed.execution, causes: [{ ...cause, scheduled_reason: 'Later' }] } });
		const finished = operatorNode({ state: 'accepted' });
		const now = Date.parse('2026-01-01T00:00:00Z');
		expect([ready, untimed, timed, scheduled, finished].map((node) => planOrderGroup(node, now, new Set()))).toEqual(['work', 'untimed', null, null, null]);
	});
});
