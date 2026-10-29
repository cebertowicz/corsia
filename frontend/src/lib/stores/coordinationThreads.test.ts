import { afterEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from './auth';
import { allThreads, coordinationThreads, threads, saveThreadTitle } from './threads';
import { togglePin } from './pinnedConversations';
import { sendMessage } from '../ws/send';
import { setWs } from '../ws/state';
import { loadThreads } from './threadsLoad';
import { handleSessionUpdated } from '../ws/handlersSession';
import { updateThreadRow } from './threadRow';

const row = { id: 'owner', user_id: 'u', profile: 'personal', title: 'CORSIA', model: null, coordination: 'corsia', created_at: '2026-01-01', updated_at: '2026-01-01' };
afterEach(() => { setWs(null); token.set(null); threads.set([]); vi.unstubAllGlobals(); });

it('loads coordination rows and clears them on sign out', async () => {
	token.set('session');
	vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ json: async () => url.includes('/api/threads?') ? { threads: [], coordination: [row] } : {} })));
	await loadThreads();
	expect(get(coordinationThreads).map((item) => item.title)).toEqual(['CORSIA']);
	token.set(null);
	expect(get(coordinationThreads)).toEqual([]);
});

it('updates coordination model without moving a newer display timestamp backwards', () => {
	allThreads.set([{ ...row, coordination: 'corsia', updated_at: '2026-02-01T00:00:00Z' }]);
	handleSessionUpdated({ thread_id: 'owner', model: 'test/model', updated_at: '2026-01-15T00:00:00Z' });
	expect(get(coordinationThreads)[0]).toMatchObject({ model: 'test/model', updated_at: '2026-02-01T00:00:00Z' });
});

it('updates coordination rows through the normal thread row writer', () => {
	allThreads.set([{ ...row, coordination: 'corsia' }]);
	updateThreadRow('owner', { reasoning_effort: 'high' });
	expect(get(coordinationThreads)[0]).toMatchObject({ reasoning_effort: 'high' });
});

it('keeps a coordination row current after send, pin, and edit', async () => {
	token.set('session');
	allThreads.set([{ ...row, coordination: 'conductor' }]);
	setWs({ readyState: WebSocket.OPEN, send: vi.fn() } as unknown as WebSocket);
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true })));
	sendMessage('owner', 'hello');
	await togglePin('owner');
	await saveThreadTitle('owner', 'Renamed', 'session');
	expect(get(allThreads)[0]).toMatchObject({ title: 'Renamed', pinned: 1, last_message: 'hello' });
	expect(get(coordinationThreads)[0]).toMatchObject({ title: 'Renamed', pinned: 1, last_message: 'hello' });
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
