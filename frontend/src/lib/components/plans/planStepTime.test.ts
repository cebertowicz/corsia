import { describe, expect, it } from 'vitest';

import type { OperatorNode } from '$lib/api/plans';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import { UNKNOWN_STEP } from './planLiveStep';
import { liveStepLabel, stepOpenQuery } from './planStepTime';

const concrete = { threadId: 't1', phrase: 'Reading plan', between: false, unknown: false, startedAt: 100 };
const unknown = { threadId: 't1', phrase: UNKNOWN_STEP, between: false, unknown: true, startedAt: 100 };

describe('liveStepLabel', () => {
	it('puts elapsed time first without repeating an unknown step', () => {
		expect(liveStepLabel(concrete, 60_000)).toBe('Working 1 min · Reading plan');
		expect(liveStepLabel(unknown, 60_000)).toBe('Working 1 min');
		expect(liveStepLabel(concrete, null)).toBe('Reading plan');
	});
});

describe('stepOpenQuery', () => {
	const attempt = (id: string, selected_at: string) => ({
		id, ordinal: 1, executor: { thread_id: 'w', label: 'W' }, requirements_revision: 1, selected_at, released_at: null, release_reason: null,
	});
	const withAttempts = (attempts: ReturnType<typeof attempt>[], executorThread?: string): OperatorNode => {
		const base = operatorNode({ id: 'n1' });
		const selection = executorThread
			? { ...(base.execution.selection ?? {}), executor_thread_id: executorThread } as never
			: base.execution.selection;
		return { ...base, execution: { ...base.execution, attempts, selection } };
	};

	it('counts from the first try on the executor thread', () => {
		const query = stepOpenQuery(withAttempts([
			attempt('b', '2026-09-26T17:59:00+00:00'), attempt('a', '2026-09-26T17:57:00+00:00'),
		], 'exec'), 'fallback');
		expect(query).toMatchObject({ threadId: 'exec', since: Date.parse('2026-09-26T17:57:00Z'), until: null });
	});

	it('asks nothing when no start is proven', () => {
		expect(stepOpenQuery(withAttempts([]), 'fallback')).toBeNull();
	});
});
