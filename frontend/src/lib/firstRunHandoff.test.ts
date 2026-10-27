import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createFirstRunHandoff } from './firstRunHandoff';
import { activeThreadId } from '$lib/stores/threads';
import { _resetFirstRunLandingForTests } from '$lib/utils/conductor';

const target = { target: { thread_id: 'first-run' }, first_run_pending: true };

describe('first-run handoff', () => {
	beforeEach(() => {
		activeThreadId.set(null);
		_resetFirstRunLandingForTests();
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(target))));
	});
	afterEach(() => {
		vi.unstubAllGlobals();
		_resetFirstRunLandingForTests();
	});

	it('keeps its own selection owner through an active-thread update', async () => {
		const handoff = createFirstRunHandoff();
		let state = { authenticated: true, token: 'session', scope: null, selectionIntent: 0, activeThread: null as string | null };
		const initialOpen = vi.fn().mockResolvedValue(false);
		handoff.startLookup({
			token: state.token, scope: state.scope, selectionIntent: state.selectionIntent,
			isActive: () => true, current: () => state, onLookup: () => {}, open: initialOpen,
		});
		await vi.waitFor(() => expect(initialOpen).toHaveBeenCalled());
		await new Promise(resolve => setTimeout(resolve, 0));

		handoff.setupFinished(state.selectionIntent);
		let owner: (() => boolean) | undefined;
		let finishOpen!: (value: boolean) => void;
		const opening = new Promise<boolean>((resolve) => { finishOpen = resolve; });
		const cancel = handoff.landAfterSetup({
			panelReady: true, activeThread: state.activeThread, current: () => state,
			open: (_id, candidate) => {
				owner = candidate;
				return opening;
			},
		});
		await vi.waitFor(() => expect(owner).toBeDefined());

		state = { ...state, activeThread: 'first-run' };
		cancel?.();
		expect(owner!()).toBe(true);
		finishOpen(true);
	});
});
