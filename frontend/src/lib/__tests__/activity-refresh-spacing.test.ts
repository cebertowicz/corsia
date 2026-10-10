/**
 * An Activity key matches every workspace plan write, and each read rebuilds a
 * whole scope: its request starts are spaced, the latest state wins, and the
 * last change is never dropped.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { token } from '$lib/stores/auth';
import { cancelAll, entryFor, refresh, resetPlanRuntimeForTests, retire } from '$lib/stores/plans.svelte';
import { ACTIVITY_MIN_GAP_MS, cancelDeferred } from '$lib/stores/planRefreshSpacing';

/** A server whose state advances with each plan write the test performs. */
function server() {
	const state = { version: 0, reads: 0 };
	const activity = () => ({
		contract_version: 1,
		representation: 'activity_scope_index',
		as_of: 'now',
		target: { kind: 'workspace_scope', id: 'workspace', label: 'Workspace' },
		revision_token: `v${state.version}`,
		plans: [],
		attention: { awaiting_count: 0 },
	});
	const operator = () => ({
		contract_version: 2,
		as_of: 'now',
		target: { kind: 'thread', id: 't1', label: 'Thread' },
		revision_token: `v${state.version}`,
		plans: [],
		attention: { awaiting_count: 0 },
	});
	const fetchMock = vi.fn(async (url: string) => {
		state.reads += 1;
		return {
			ok: true,
			status: 200,
			json: async () => (String(url).includes('/activity/') ? activity() : operator()),
			headers: { get: () => `"v${state.version}"` },
		};
	});
	vi.stubGlobal('fetch', fetchMock);
	return { state, fetchMock };
}

const WORKSPACE = { kind: 'activity-workspace' } as const;
const seen = () => entryFor('activity:workspace').payload?.revision_token;

beforeEach(() => {
	vi.useFakeTimers();
	vi.unstubAllGlobals();
	resetPlanRuntimeForTests();
	token.set('bearer-1');
});
afterEach(() => {
	resetPlanRuntimeForTests();
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe('Activity key refresh spacing', () => {
	it('reads at once the first time, so opening the page is never delayed', async () => {
		const { fetchMock } = server();
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(0);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(seen()).toBe('v0');
	});

	it('turns a burst of writes into one read now and one trailing read that sees the last write', async () => {
		const { state, fetchMock } = server();
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(0);
		fetchMock.mockClear();

		for (let write = 1; write <= 50; write += 1) {
			state.version = write;
			void refresh(WORKSPACE);
			await vi.advanceTimersByTimeAsync(10);
		}
		expect(fetchMock).not.toHaveBeenCalled();

		await vi.advanceTimersByTimeAsync(ACTIVITY_MIN_GAP_MS);
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(seen()).toBe('v50');
	});

	it('does not drop a write that lands while the trailing read is in flight', async () => {
		const { state, fetchMock } = server();
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(0);
		state.version = 1;
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(ACTIVITY_MIN_GAP_MS);
		expect(seen()).toBe('v1');

		state.version = 2;
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(ACTIVITY_MIN_GAP_MS);

		expect(seen()).toBe('v2');
		expect(fetchMock).toHaveBeenCalledTimes(3);
	});

	it('keeps a minute of constant writes to one read per gap, ending on the final state', async () => {
		const { state, fetchMock } = server();
		const minute = 60_000;
		for (let elapsed = 0; elapsed < minute; elapsed += 100) {
			state.version += 1;
			void refresh(WORKSPACE);
			await vi.advanceTimersByTimeAsync(100);
		}
		await vi.advanceTimersByTimeAsync(ACTIVITY_MIN_GAP_MS);

		// 600 writes in the minute; unspaced, each would have read.
		expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(32);
		expect(seen()).toBe(`v${state.version}`);
	});

	it('spaces each Activity key on its own', async () => {
		const { fetchMock } = server();
		void refresh(WORKSPACE);
		void refresh({ kind: 'activity-project', id: 'p1' });
		await vi.advanceTimersByTimeAsync(0);
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('leaves operator keys back to back as before', async () => {
		const { state, fetchMock } = server();
		void refresh({ kind: 'thread', id: 't1' });
		await vi.advanceTimersByTimeAsync(0);
		state.version = 1;
		void refresh({ kind: 'thread', id: 't1' });
		await vi.advanceTimersByTimeAsync(0);

		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(entryFor('thread:t1').payload?.revision_token).toBe('v1');
	});

	it('reads at once after an auth change, whatever just ran', async () => {
		const { fetchMock } = server();
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(0);
		fetchMock.mockClear();

		cancelAll();
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(0);

		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('runs no trailing read for a key that is retired or no longer observed', async () => {
		const { fetchMock } = server();
		void refresh(WORKSPACE);
		await vi.advanceTimersByTimeAsync(0);
		fetchMock.mockClear();

		const waiting = refresh(WORKSPACE);
		retire(WORKSPACE);
		await waiting;
		void refresh({ kind: 'activity-project', id: 'p1' });
		await vi.advanceTimersByTimeAsync(0);
		fetchMock.mockClear();
		const dropped = refresh({ kind: 'activity-project', id: 'p1' });
		cancelDeferred('activity-project:p1');
		await dropped;
		await vi.advanceTimersByTimeAsync(ACTIVITY_MIN_GAP_MS * 2);

		expect(fetchMock).not.toHaveBeenCalled();
	});
});
