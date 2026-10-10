import { describe, expect, it } from 'vitest';

import type { OperatorNode } from '$lib/api/plans';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import {
	NO_LIVE_LINE,
	UNKNOWN_STEP,
	_resetLiveStepForTests,
	advanceLiveLine,
	carriedByParent,
	liveStepLabel,
	rolledLive,
} from './planLiveStep';

const concrete = { threadId: 't1', phrase: 'Reading plan', between: false, unknown: false, startedAt: 100 };
const unknown = { threadId: 't1', phrase: UNKNOWN_STEP, between: false, unknown: true, startedAt: 100 };

describe('liveStepLabel', () => {
	it('puts elapsed time first without repeating an unknown step', () => {
		expect(liveStepLabel(concrete, 60_100)).toBe('Working 1 min · Reading plan');
		expect(liveStepLabel(unknown, 60_100)).toBe('Working 1 min');
		expect(liveStepLabel({ ...concrete, startedAt: null }, 60_100)).toBe('Reading plan');
	});
});

describe('advanceLiveLine', () => {
	it('drops a thread phrase when its running turn ends', () => {
		_resetLiveStepForTests();
		const running = advanceLiveLine(NO_LIVE_LINE, concrete, false, 100).line;
		advanceLiveLine(running, null, false, 200);

		const restarted = advanceLiveLine(NO_LIVE_LINE, unknown, false, 300);

		expect(restarted.show?.phrase).toBe(UNKNOWN_STEP);
	});
});

/** A step delegated to `threadId`, under `parentId`. */
function step(id: string, parentId: string | null, threadId: string | null): OperatorNode {
	const base = operatorNode({ id, parent_id: parentId });
	if (!threadId) return base;
	return { ...base, child: { available: true, plan_id: `${id}-plan`, thread_id: threadId, progress: { done: 0, total: 1, open: 1, liveness: 'advancing' } } };
}

/** How the server shows a step: its label, and its own condition. */
function shown(node: OperatorNode, label: 'working' | 'waiting', condition: OperatorNode['execution']['condition']): OperatorNode {
	const tone = label === 'working' ? 'active' : 'waiting';
	return { ...node, display: { label_key: label, tone_key: tone }, execution: { ...node.execution, condition } };
}

describe('rolledLive', () => {
	it('gives every ancestor its descendants\' threads, its own first, in plan order', () => {
		const { threads: rolled } = rolledLive([
			step('root', null, null), step('mid', 'root', 'w-mid'), step('leaf', 'mid', 'w-leaf'), step('side', 'root', 'w-side'),
		]);

		expect(rolled.get('root')).toEqual(['w-mid', 'w-leaf', 'w-side']);
		expect(rolled.get('mid')).toEqual(['w-mid', 'w-leaf']);
		expect(rolled.get('leaf')).toEqual(['w-leaf']);
	});

	it('ends on a parent cycle instead of walking it forever', () => {
		const { threads: rolled } = rolledLive([step('a', 'b', 'w-a'), step('b', 'a', null)]);

		expect(rolled.get('b')).toEqual(['w-a']);
		expect(rolled.get('a')).toEqual(['w-a']);
	});

	it('moves a step the server shows Working, and every branch over it, at any depth', () => {
		// A delegated step waits on its worker; the server reads it Working while work moves below that worker.
		const { moving } = rolledLive([
			shown(step('root', null, null), 'working', 'ready'),
			shown(step('mid', 'root', null), 'working', 'ready'),
			shown(step('leaf', 'mid', 'w-leaf'), 'working', 'waiting'),
			shown(step('side', 'root', 'w-side'), 'waiting', 'waiting'),
		]);

		expect([...moving].sort()).toEqual(['leaf', 'mid', 'root']);
	});

	it('does not move a step that reads Working while it awaits review, nor a branch for its own label', () => {
		const { moving } = rolledLive([
			shown(step('root', null, null), 'working', 'ready'),
			shown(step('reviewed', 'root', null), 'working', 'review'),
		]);

		expect(moving.size).toBe(0);
	});
});

describe('carriedByParent', () => {
	it('names what each row\'s rendered children already show', () => {
		const nodes = [step('root', null, 'w-root'), step('kid', 'root', 'w-kid'), step('hidden', 'root', 'w-hidden')];
		const { threads } = rolledLive(nodes);

		// `hidden` is not rendered, so the parent still owes its line.
		expect(carriedByParent(nodes.slice(0, 2), threads).get('root')).toEqual(['w-kid']);
		expect(carriedByParent(nodes, threads).get('root')).toEqual(['w-kid', 'w-hidden']);
		expect(carriedByParent(nodes, threads).has('kid')).toBe(false);
	});
});
