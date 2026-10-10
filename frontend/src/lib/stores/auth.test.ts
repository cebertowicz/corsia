import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { isAuthenticated, logout, token, user } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';

beforeEach(() => {
	token.set('a-session-token');
	user.set({ id: 'demo', name: 'demo' });
});

afterEach(() => {
	vi.restoreAllMocks();
	token.set(null);
	user.set(null);
});

describe('logout', () => {
	it('posts to /api/auth/logout with the bearer token, then clears local state', async () => {
		const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) }));
		vi.stubGlobal('fetch', fetchMock);

		await logout();

		expect(fetchMock).toHaveBeenCalledWith('/api/auth/logout', {
			method: 'POST',
			headers: { Authorization: 'Bearer a-session-token' },
		});
		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(get(isAuthenticated)).toBe(false);
	});

	it('still clears local state when the server call fails, and warns instead of swallowing it', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));

		await logout();

		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(warn).toHaveBeenCalled();
	});

	it('still clears local state on a non-ok response, and warns', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));

		await logout();

		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(warn).toHaveBeenCalled();
	});

	it('tells the user when the session could not be revoked', async () => {
		vi.spyOn(console, 'warn').mockImplementation(() => {});
		const show = vi.spyOn(toasts, 'show');
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));

		await logout();

		expect(show).toHaveBeenCalledWith(
			expect.stringContaining('could not end the session'),
			'error',
			expect.any(Number),
		);
	});

	it('stays quiet when the session was already invalid', async () => {
		vi.spyOn(console, 'warn').mockImplementation(() => {});
		const show = vi.spyOn(toasts, 'show');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401 })));

		await logout();

		expect(show).not.toHaveBeenCalled();
		expect(get(user)).toBeNull();
	});

	it('clears local state without a network call when there is no token', async () => {
		token.set(null);
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);

		await logout();

		expect(fetchMock).not.toHaveBeenCalled();
		expect(get(user)).toBeNull();
	});
});
