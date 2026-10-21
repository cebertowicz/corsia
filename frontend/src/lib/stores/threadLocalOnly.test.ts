import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { threads, type Thread } from './threads';
import { saveThreadLocalOnly, threadLocalOnly } from './threadLocalOnly';

function makeThread(overrides: Partial<Thread> = {}): Thread {
	return {
		id: 't1', user_id: 'u1', profile: 'personal', title: 'Test', model: null,
		created_at: 'now', updated_at: 'now', ...overrides,
	};
}

function reloadBody(overrides: Partial<Thread> = {}) {
	return { threads: [makeThread(overrides)] };
}

afterEach(() => {
	threads.set([]);
	vi.unstubAllGlobals();
});

describe('threadLocalOnly', () => {
	it('reads an ordinary thread as off', () => {
		expect(threadLocalOnly(makeThread())).toEqual({ effective: false, cause: 'off', inheritedFromThreadId: null, inheritedFromTitle: null });
		expect(threadLocalOnly(undefined).effective).toBe(false);
	});
	it('reads the server-provided cause without deriving an order', () => {
		const inherited = makeThread({ local_only_effective: true, local_only_cause: 'inherited', local_only_inherited_from_thread_id: 'p1', local_only_inherited_from_title: 'Quarterly report' });
		expect(threadLocalOnly(inherited)).toEqual({ effective: true, cause: 'inherited', inheritedFromThreadId: 'p1', inheritedFromTitle: 'Quarterly report' });
		expect(threadLocalOnly(makeThread({ local_only_effective: true, local_only_cause: 'own' }))).toMatchObject({ effective: true, cause: 'own' });
		expect(threadLocalOnly(makeThread({ local_only_effective: true, local_only_cause: 'project' }))).toMatchObject({ effective: true, cause: 'project' });
	});
	it('does not invent a cause from raw fields', () => {
		expect(threadLocalOnly(makeThread({ local_only: 1, local_only_effective: true }))).toMatchObject({ effective: true, cause: 'off' });
		expect(threadLocalOnly(makeThread({ local_only: 1, local_only_cause: 'own' }))).toMatchObject({ effective: false, cause: 'own' });
	});
});

describe('saveThreadLocalOnly', () => {
	it('writes through the person route and reloads the authoritative thread list', async () => {
		threads.set([makeThread()]);
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => String(input).startsWith('/api/threads?')
			? ({ ok: true, json: async () => reloadBody({ local_only: true, local_only_effective: true, local_only_cause: 'own' }) })
			: ({ ok: true }));
		vi.stubGlobal('fetch', fetchMock);
		expect(await saveThreadLocalOnly('t1', true, 'tok')).toEqual({ ok: true });
		expect(fetchMock).toHaveBeenCalledWith('/api/threads/t1/local-only', expect.objectContaining({
			method: 'PUT', body: JSON.stringify({ local_only: true }), headers: expect.objectContaining({ Authorization: 'Bearer tok' }),
		}));
		expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/threads?'));
		expect(get(threads)[0]).toMatchObject({ local_only: true, local_only_effective: true, local_only_cause: 'own' });
	});
	it('leaves the store alone and returns the save-failure line when the server cannot be reached', async () => {
		threads.set([makeThread()]);
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		expect(await saveThreadLocalOnly('t1', true, 'tok')).toEqual({ ok: false, error: "Couldn’t save this change. Check your connection and try again." });
		expect(get(threads)[0].local_only).toBeUndefined();
	});
	it('reports a successful save whose required reload failed', async () => {
		threads.set([makeThread()]);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input).startsWith('/api/threads?')
			? ({ ok: true, json: async () => { throw new SyntaxError('bad JSON'); } })
			: ({ ok: true })));
		expect(await saveThreadLocalOnly('t1', true, 'tok')).toEqual({ ok: false, error: 'Saved. Reload the page to see it.' });
		expect(get(threads)[0].local_only).toBeUndefined();
	});
	it('names the server\'s reason on a refusal', async () => {
		threads.set([makeThread()]);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ code: 'local_only_set_by_parent', message: 'Turn Local only off in the chat that started this one.' }) })));
		expect(await saveThreadLocalOnly('t1', false, 'tok')).toEqual({ ok: false, error: "Couldn’t save this change. Turn Local only off in the chat that started this one." });
		expect(get(threads)[0].local_only).toBeUndefined();
	});
});
