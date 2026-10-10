import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const wsMocks = vi.hoisted(() => ({ connect: vi.fn() }));
vi.mock('$lib/ws', () => ({ connect: wsMocks.connect }));

import LoginScreen from './LoginScreen.svelte';
import { token, user } from '$lib/stores/auth';
import { connectionError } from '$lib/stores/connection';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function statusResponse(hasUsers: boolean) {
	return { ok: true, json: async () => ({ has_users: hasUsers }) };
}

function usernameField() {
	return host.querySelector<HTMLInputElement>('input[placeholder="Username"]');
}
function passwordField() {
	return host.querySelector<HTMLInputElement>('input[placeholder="Password"]');
}
function submitButton() {
	return host.querySelector<HTMLButtonElement>('button[type="submit"]');
}
function setInputValue(input: HTMLInputElement, value: string) {
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
}

function mountScreen() {
	component = mount(LoginScreen, { target: host });
	flushSync();
}

beforeEach(() => {
	wsMocks.connect.mockClear();
	token.set(null);
	user.set(null);
	connectionError.set(null);
	window.history.replaceState({}, '', '/');
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	user.set(null);
	connectionError.set(null);
});

describe('LoginScreen', () => {
	it('shows the no-account state with the CLI command instead of a form', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(false)));
		mountScreen();

		await vi.waitFor(() => expect(host.textContent).toContain('corsia user add'));
		expect(usernameField()).toBeNull();
		expect(host.textContent).toContain('Check again');
	});

	it('re-checks status when Check again is clicked and shows the form once an account exists', async () => {
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(statusResponse(false))
			.mockResolvedValueOnce(statusResponse(true));
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(host.textContent).toContain('Check again'));

		const checkAgain = Array.from(host.querySelectorAll('button'))
			.find((b) => b.textContent?.includes('Check again'));
		checkAgain!.click();
		flushSync();

		await vi.waitFor(() => expect(usernameField()).not.toBeNull());
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('signs in on success: posts JSON credentials, stores the token, and connects', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url === '/api/auth/status') return statusResponse(true);
			if (url === '/api/auth/login') {
				expect(init?.method).toBe('POST');
				expect(init?.headers).toMatchObject({ 'Content-Type': 'application/json' });
				expect(JSON.parse(String(init?.body))).toEqual({ username: 'demo', password: 'hunter2' });
				return {
					ok: true, status: 200,
					json: async () => ({
						token: 'new-session-token',
						user: { id: 'demo', name: 'demo' },
						expires_at: '2026-10-01T00:00:00Z',
					}),
				};
			}
			throw new Error(`unexpected fetch ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		setInputValue(usernameField()!, 'demo');
		setInputValue(passwordField()!, 'hunter2');
		flushSync();
		submitButton()!.click();

		await vi.waitFor(() => expect(get(token)).toBe('new-session-token'));
		expect(wsMocks.connect).toHaveBeenCalled();
	});

	it('shows a plain message and clears only the password on 401, keeping the username', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/auth/status') return statusResponse(true);
			return { ok: false, status: 401, json: async () => ({ detail: 'Invalid username or password' }) };
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		setInputValue(usernameField()!, 'demo');
		setInputValue(passwordField()!, 'wrong');
		flushSync();
		submitButton()!.click();

		await vi.waitFor(() => expect(host.textContent).toContain('Wrong username or password.'));
		expect(usernameField()!.value).toBe('demo');
		expect(passwordField()!.value).toBe('');
		expect(get(token)).toBeNull();
	});

	it('shows the server detail on 429', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/auth/status') return statusResponse(true);
			return {
				ok: false, status: 429,
				json: async () => ({ detail: 'Too many attempts. Try again in 30 seconds.' }),
			};
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		setInputValue(usernameField()!, 'demo');
		setInputValue(passwordField()!, 'hunter2');
		flushSync();
		submitButton()!.click();

		await vi.waitFor(() => expect(host.textContent).toContain('Too many attempts. Try again in 30 seconds.'));
	});

	it("shows a network-failure message when the login request can't reach the server", async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/auth/status') return statusResponse(true);
			throw new Error('network down');
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		setInputValue(usernameField()!, 'demo');
		setInputValue(passwordField()!, 'hunter2');
		flushSync();
		submitButton()!.click();

		await vi.waitFor(() => expect(host.textContent).toContain("Can't reach CORSIA."));
	});

	it('disables the submit button while a login request is in flight', async () => {
		let resolveLogin: (value: unknown) => void = () => {};
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/auth/status') return statusResponse(true);
			return new Promise((resolve) => { resolveLogin = resolve; });
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		setInputValue(usernameField()!, 'demo');
		setInputValue(passwordField()!, 'hunter2');
		flushSync();
		submitButton()!.click();
		flushSync();

		expect(submitButton()!.disabled).toBe(true);

		resolveLogin({
			ok: true, status: 200,
			json: async () => ({ token: 't', user: { id: 'demo', name: 'demo' }, expires_at: '2026-10-01T00:00:00Z' }),
		});
		await vi.waitFor(() => expect(get(token)).toBe('t'));
	});

	it('signs in directly via the ?token= URL path without calling the login endpoint', async () => {
		window.history.replaceState({}, '', '/?token=url-token-abc');
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(true)));
		mountScreen();

		await vi.waitFor(() => expect(get(token)).toBe('url-token-abc'));
		expect(wsMocks.connect).toHaveBeenCalled();
		expect(window.location.search).toBe('');
	});

	it('shows the ended-session message when the socket reports an invalid or expired session', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(true)));
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		connectionError.set('Invalid or expired session');
		flushSync();

		expect(host.textContent).toContain('Your session ended. Sign in again.');
	});
});
