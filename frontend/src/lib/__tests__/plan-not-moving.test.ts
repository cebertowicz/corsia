import { describe, expect, it } from 'vitest';
import {
	cardStateLabel,
	cardStateTone,
	planCardState,
} from '$lib/components/plans/planDisplay';
import type { OperatorNode, OperatorPlanEntry } from '$lib/api/plans';
import { operatorNode, planDisplay, withPlanContract } from './fixtures/operatorPlan';

function entry(nodes: OperatorNode[], display: OperatorPlanEntry['display'] = planDisplay('waiting', 'waiting')): OperatorPlanEntry {
	return withPlanContract({
		plan: {
			id: 'p1', owner_kind: 'thread', owner_id: 't1',
			state: 'active', revision: 1, updated_at: 'now',
		},
		owner: { label: 'Thread', project_id: null, thread_id: 't1' },
		progress: { done: 0, total: nodes.length, open: nodes.length, liveness: 'waiting' },
		display,
		current_node_ids: [],
		next_node_ids: [],
		nodes,
	});
}

describe('plan display state', () => {
	it('reads the server four-state key and tone', () => {
		const value = entry([], planDisplay('needs_you', 'human-review'));
		expect(cardStateLabel(planCardState(value))).toBe('Needs you');
		expect(cardStateTone(value)).toBe('human-review');
	});
});
