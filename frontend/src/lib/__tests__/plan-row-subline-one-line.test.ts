import { describe, expect, it } from 'vitest';
import { nodeSubline } from '$lib/components/plans/planDisplay';
import { operatorNode } from './fixtures/operatorPlan';

// The row renders its subline with `white-space: pre`, so any line break in it stacks.
describe('a row subline', () => {
	it('stays one line when its reason carries several', () => {
		const node = operatorNode({
			id: 'sent-back',
			status_reason: 'Sent back:\nB1: a stray gap\n\n  B2: the pin drifts\r\nR1: after send',
		});
		expect(nodeSubline(node)).toBe('Sent back: B1: a stray gap B2: the pin drifts R1: after send');
	});
});
