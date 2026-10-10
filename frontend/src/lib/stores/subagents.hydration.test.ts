import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	_resetSubagentHydrationForTests,
	hydrateSubagentThread,
	hydrateSubagentThreadOnce,
	registerSubagent,
	setSubagentStatus,
	subagentMeta,
} from './subagents';

/**
 * A reload empties an in-memory map. What the SERVER knows about a worker
 * survives it, so the chip re-reads runtime truth instead of falling back to a
 * word it cannot support.
 */

const TOKEN = 'test-token';

function runtimeTruth(overrides: Record<string, unknown> = {}) {
	return {
		thread_id: 'child-1',
		title: 'tmp inspector',
		thread_kind: 'subagent',
		driver: 'cc',
		model: 'cc/opus',
		cc_live: false,
		codex_live: false,
		busy: false,
		turn_active: false,
		...overrides,
	};
}

function respondWith(body: unknown, ok = true): void {
	vi.stubGlobal('fetch', vi.fn(async () => ({
		ok,
		status: ok ? 200 : 500,
		json: async () => body,
	})) as never);
}

beforeEach(() => {
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
});

describe('subagent chip hydration reads server runtime truth', () => {
	it('a worker with a turn in flight is running', async () => {
		respondWith(runtimeTruth({ busy: true, turn_active: true }));
		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');
		expect(get(subagentMeta).get('child-1')).toMatchObject({
			parentThreadId: 'parent-1',
			name: 'tmp inspector',
			provider: 'cc',
			status: 'running',
			model: 'cc/opus',
		});
	});

	it('a live process between turns is ready', async () => {
		respondWith(runtimeTruth({ cc_live: true }));
		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');
		expect(get(subagentMeta).get('child-1')?.status).toBe('ready');
	});

	it('a worker with no live runtime is idle — never an outcome we cannot see', async () => {
		respondWith(runtimeTruth());
		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');
		expect(get(subagentMeta).get('child-1')?.status).toBe('idle');
	});

	it('never overwrites a terminal outcome observed live', async () => {
		registerSubagent('parent-1', 'child-1', 'tmp inspector', 'cc', 'closed');
		respondWith(runtimeTruth());
		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');
		expect(get(subagentMeta).get('child-1')?.status).toBe('closed');
	});

	it('a live frame that lands mid-request wins over the response', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => {
			registerSubagent('parent-1', 'child-1', 'tmp inspector', 'cc', 'running');
			return { ok: true, status: 200, json: async () => runtimeTruth() };
		}) as never);

		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');

		expect(get(subagentMeta).get('child-1')?.status).toBe('running');
	});

	it('leaves an ordinary chat thread out of the subagent map', async () => {
		respondWith(runtimeTruth({ thread_kind: 'chat' }));
		await hydrateSubagentThread('peer-1', TOKEN, 'parent-1');
		expect(get(subagentMeta).has('peer-1')).toBe(false);
	});

	it('evicts a thread the server says is not a worker at all', async () => {
		registerSubagent('parent-1', 'peer-1', 'Peer', 'cc', 'unknown');
		respondWith(runtimeTruth({ thread_kind: 'chat' }));
		await hydrateSubagentThread('peer-1', TOKEN, 'parent-1');
		// Membership decides send routing, so a wrong entry is not cosmetic.
		expect(get(subagentMeta).has('peer-1')).toBe(false);
	});

	it('declares a failed read at error level and claims nothing', async () => {
		const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
		respondWith({}, false);

		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');

		expect(get(subagentMeta).has('child-1')).toBe(false);
		expect(logged).toHaveBeenCalled();
	});

	it('never downgrades known parent linkage to nothing', async () => {
		registerSubagent('parent-1', 'child-1', 'tmp inspector', 'cc', 'unknown');
		respondWith(runtimeTruth({ busy: true }));
		await hydrateSubagentThread('child-1', TOKEN, '');
		expect(get(subagentMeta).get('child-1')?.parentThreadId).toBe('parent-1');
	});

	it('hydrates a thread once per page load, and retries after a failure', async () => {
		respondWith(runtimeTruth());
		hydrateSubagentThreadOnce('child-1', TOKEN, 'parent-1');
		hydrateSubagentThreadOnce('child-1', TOKEN, 'parent-1');
		await vi.waitFor(() => expect(get(subagentMeta).has('child-1')).toBe(true));
		expect(globalThis.fetch).toHaveBeenCalledTimes(1);

		subagentMeta.set(new Map());
		respondWith({}, false);
		vi.spyOn(console, 'error').mockImplementation(() => {});
		await hydrateSubagentThread('child-2', TOKEN, 'parent-1');
		respondWith(runtimeTruth({ thread_id: 'child-2' }));
		hydrateSubagentThreadOnce('child-2', TOKEN, 'parent-1');
		await vi.waitFor(() => expect(get(subagentMeta).has('child-2')).toBe(true));
	});

	it('keeps the open thread routable as a subagent after a reload', async () => {
		respondWith(runtimeTruth({ thread_id: 'child-1', cc_live: true }));
		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');
		// ws/send.ts and the composer both branch on membership in this map.
		expect(get(subagentMeta).has('child-1')).toBe(true);
	});

	it('records the parent the server names when the caller knows none', async () => {
		respondWith(runtimeTruth({ parent_thread_id: 'parent-9' }));
		await hydrateSubagentThread('child-1', TOKEN, '');
		expect(get(subagentMeta).get('child-1')?.parentThreadId).toBe('parent-9');
	});

	it('records the server parent even when a terminal status keeps its word', async () => {
		registerSubagent('', 'child-1', 'tmp inspector', 'cc', 'closed');
		respondWith(runtimeTruth({ parent_thread_id: 'parent-9', busy: true }));
		await hydrateSubagentThread('child-1', TOKEN, '');
		expect(get(subagentMeta).get('child-1')).toMatchObject({ parentThreadId: 'parent-9', status: 'closed' });
	});

	it('never registers a visible thread because it has a lineage parent', async () => {
		respondWith(runtimeTruth({ thread_kind: 'chat', parent_thread_id: 'conductor' }));
		await hydrateSubagentThread('coordinator', TOKEN, '');
		expect(get(subagentMeta).has('coordinator')).toBe(false);
	});

	it('a later live status still moves a hydrated chip', async () => {
		respondWith(runtimeTruth({ busy: true }));
		await hydrateSubagentThread('child-1', TOKEN, 'parent-1');
		setSubagentStatus('child-1', 'closed');
		expect(get(subagentMeta).get('child-1')?.status).toBe('closed');
	});
});
