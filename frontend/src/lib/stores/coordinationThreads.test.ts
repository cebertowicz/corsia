import { afterEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from './auth';
import { coordinationThreads, threads } from './threads';
import { loadThreads } from './threadsLoad';
import { handleSessionUpdated } from '../ws/handlersSession';

const row = { id: 'owner', user_id: 'u', profile: 'personal', title: 'CORSIA', model: null, coordination: 'corsia', created_at: '2026-01-01', updated_at: '2026-01-01' };
afterEach(() => { token.set(null); threads.set([]); vi.unstubAllGlobals(); });

it('loads coordination rows and clears them on sign out', async () => {
	token.set('session');
	vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ json: async () => url.includes('/api/threads?') ? { threads: [], coordination: [row] } : {} })));
	await loadThreads();
	expect(get(coordinationThreads).map((item) => item.title)).toEqual(['CORSIA']);
	token.set(null);
	expect(get(coordinationThreads)).toEqual([]);
});

it('applies a conductor rename from session_updated without reloading the list', () => {
	coordinationThreads.set([{ ...row, title: 'Old Conductor', coordination: 'conductor' }]);
	handleSessionUpdated({ thread_id: 'owner', title: 'New Conductor' });
	expect(get(coordinationThreads)[0].title).toBe('New Conductor');
});

it('does not restore a pending list after sign out', async () => {
	token.set('session');
	let finish!: (value: unknown) => void;
	vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { finish = resolve; })));
	const pending = loadThreads();
	token.set(null);
	finish({ json: async () => ({ threads: [], coordination: [row] }) });
	await pending;
	expect(get(coordinationThreads)).toEqual([]);
});
