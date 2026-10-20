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

afterEach(() => {
	threads.set([]);
	vi.unstubAllGlobals();
});

describe('threadLocalOnly', () => {
	it('reads an ordinary thread as not Local only', () => {
		expect(threadLocalOnly(makeThread())).toEqual({ effective: false, own: false, inheritedFromThreadId: null, inheritedFromTitle: null });
		expect(threadLocalOnly(undefined).effective).toBe(false);
	});
	it('reads the stored value as the thread\'s own until the payload names an effective one', () => {
		expect(threadLocalOnly(makeThread({ local_only: 1 }))).toMatchObject({ effective: true, own: true });
		expect(threadLocalOnly(makeThread({ local_only: true }))).toMatchObject({ effective: true, own: true });
	});
	it('reads an inherited worker with its parent and title', () => {
		const worker = makeThread({ local_only: 1, local_only_inherited_from_thread_id: 'p1', local_only_inherited_from_title: 'Quarterly report' });
		expect(threadLocalOnly(worker)).toEqual({ effective: true, own: false, inheritedFromThreadId: 'p1', inheritedFromTitle: 'Quarterly report' });
	});
	it('prefers local_only_effective, so a project-only cause is effective without being own', () => {
		expect(threadLocalOnly(makeThread({ local_only: 0, local_only_effective: true }))).toMatchObject({ effective: true, own: false });
		expect(threadLocalOnly(makeThread({ local_only: 1, local_only_effective: false })).effective).toBe(false);
	});
});

describe('saveThreadLocalOnly', () => {
	it('writes through the person route and changes the store only once confirmed', async () => {
		threads.set([makeThread()]);
		let during: Thread[] = [];
		const fetchMock = vi.fn(async () => { during = get(threads); return { ok: true, json: async () => ({ ok: true, local_only: true }) }; });
		vi.stubGlobal('fetch', fetchMock);
		expect(await saveThreadLocalOnly('t1', true, 'tok')).toEqual({ ok: true });
		expect(fetchMock).toHaveBeenCalledWith('/api/threads/t1/local-only', expect.objectContaining({
			method: 'PUT',
			body: JSON.stringify({ local_only: true }),
			headers: expect.objectContaining({ Authorization: 'Bearer tok' }),
		}));
		expect(during[0].local_only).toBeUndefined();
		expect(get(threads)[0]).toMatchObject({ local_only: true, local_only_effective: true });
	});
	it('leaves the store alone and returns the save-failure line when the server cannot be reached', async () => {
		threads.set([makeThread()]);
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		expect(await saveThreadLocalOnly('t1', true, 'tok')).toEqual({ ok: false, error: "Couldn't save this change. Check your connection and try again." });
		expect(get(threads)[0].local_only).toBeUndefined();
	});
	it('names the server\'s reason on a refusal', async () => {
		threads.set([makeThread()]);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ code: 'local_only_set_by_parent', message: 'Turn Local only off in the chat that started this one.' }) })));
		expect(await saveThreadLocalOnly('t1', false, 'tok')).toEqual({ ok: false, error: "Couldn't save this change. Turn Local only off in the chat that started this one. Try again." });
		expect(get(threads)[0].local_only).toBeUndefined();
	});
});
