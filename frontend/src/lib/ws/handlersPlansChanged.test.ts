import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ read: vi.fn() }));

vi.mock('$lib/api/plans', () => ({ readThreadPlanStates: mocks.read }));

import { handleMessage } from './handlers';
import { on } from './events';
import { token } from '../stores/auth';
import {
	resetThreadPlanStatesForTests,
	threadPlanStates,
	watchThreadPlanStates,
} from '../stores/threadPlanStates';

/** One server frame of the shape `backend/plans/notifications.py` emits. */
function frame(ownerId: string): Record<string, unknown> {
	return {
		type: 'plans_changed',
		contract_version: 1,
		plans: [{
			plan_id: 'plan-1',
			revision: 2,
			owner: { kind: 'thread', id: ownerId },
			scope: { workspace_id: null, project_id: 'project-1' },
		}],
	};
}

async function settle(): Promise<void> {
	for (let i = 0; i < 5; i++) await Promise.resolve();
}

beforeEach(() => {
	mocks.read.mockReset();
	token.set('bearer');
});

afterEach(() => {
	resetThreadPlanStatesForTests();
	token.set(null);
});

describe('plans_changed frames reaching consumers', () => {
	it('delivers the frame to subscribers instead of dropping it', () => {
		const seen: unknown[] = [];
		const off = on('plans_changed', (message: unknown) => seen.push(message));
		try {
			handleMessage(frame('a'), () => {});
		} finally {
			off();
		}
		expect(seen).toHaveLength(1);
		expect(seen[0]).toMatchObject({ type: 'plans_changed' });
	});

	it('moves the chat list mark to the paused reading when the frame arrives', async () => {
		mocks.read.mockResolvedValueOnce({ a: { state: 'moving', label_key: 'working' } });
		watchThreadPlanStates(['a']);
		await settle();
		expect(get(threadPlanStates).a?.state).toBe('moving');

		// The pause is applied, the server sends the nudge, and the chat list
		// must re-read without waiting for a reconnect.
		mocks.read.mockResolvedValueOnce({ a: { state: 'paused', label_key: 'waiting' } });
		handleMessage(frame('a'), () => {});
		await settle();
		expect(get(threadPlanStates).a?.state).toBe('paused');
	});
});