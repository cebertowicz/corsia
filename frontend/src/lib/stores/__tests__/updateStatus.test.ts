import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from '$lib/stores/auth';
import { loadUpdateStatus, updateStatus } from '$lib/stores/settings';

const AVAILABLE = {
	branch: 'dev',
	current_sha: 'aaa',
	remote_sha: 'bbb',
	commits_behind: 3,
	available: true,
	checked_at: 1,
	error: null,
};

beforeEach(() => {
	token.set('test-token');
	updateStatus.set(null);
});

afterEach(() => {
	vi.restoreAllMocks();
	token.set(null);
	updateStatus.set(null);
});

describe('loadUpdateStatus', () => {
	it('stores the reported status', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => AVAILABLE })));

		await loadUpdateStatus();

		expect(get(updateStatus)?.available).toBe(true);
		expect(get(updateStatus)?.commits_behind).toBe(3);
	});

	it('drops to unknown when the request throws, never keeping a stale answer', async () => {
		updateStatus.set({ ...AVAILABLE, available: false, commits_behind: 0 });
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));

		await loadUpdateStatus();

		const status = get(updateStatus);
		expect(status?.available).toBeNull();
		expect(status?.commits_behind).toBeNull();
		expect(status?.error).toContain('offline');
		// Last known context is preserved for display.
		expect(status?.branch).toBe('dev');
	});

	it('treats a non-OK response as unknown rather than ignoring it', async () => {
		updateStatus.set(AVAILABLE);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));

		await loadUpdateStatus();

		const status = get(updateStatus);
		expect(status?.available).toBeNull();
		expect(status?.error).toContain('500');
	});

	it('does nothing without a token', async () => {
		token.set(null);
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);

		await loadUpdateStatus();

		expect(fetchMock).not.toHaveBeenCalled();
		expect(get(updateStatus)).toBeNull();
	});
});
