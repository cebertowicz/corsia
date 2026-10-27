import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const wsMocks = vi.hoisted(() => ({ connect: vi.fn() }));
const remoteShellMocks = vi.hoisted(() => ({
	shellInfo: vi.fn(),
	disconnectRemote: vi.fn(),
}));
vi.mock('$lib/ws', () => ({ connect: wsMocks.connect }));
vi.mock('$lib/remoteShell', () => remoteShellMocks);

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

function mountScreen(props: { onNoAccount?: () => void } = {}) {
	component = mount(LoginScreen, { target: host, props });
	flushSync();
}

beforeEach(() => {
	wsMocks.connect.mockClear();
	remoteShellMocks.shellInfo.mockReturnValue(null);
	remoteShellMocks.disconnectRemote.mockReset();
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
	it.each(['Studio', ''])('names the remote on sign-in when known (%s)', async (name) => {
		remoteShellMocks.shellInfo.mockReturnValue({ mode: 'remote', name, localConfigured: true });
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(true)));
		mountScreen();
		await vi.waitFor(() => expect(submitButton()).not.toBeNull());
		expect(host.textContent).toContain(name ? 'Sign in to Studio' : 'Your personal AI assistant');
	});

	it.each(['Studio', ''])('uses a truthful status heading for response and network failures (%s)', async (name) => {
		remoteShellMocks.shellInfo.mockReturnValue({ mode: 'remote', name, localConfigured: true });
		const fetchMock = vi.fn().mockResolvedValueOnce({ ok: false, status: 500 }).mockRejectedValueOnce(new Error('network'));
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		const heading = name ? 'Can’t check Studio' : 'Can’t check the other computer';
		const expectErrorActions = () => {
			expect(host.querySelector('h1')?.textContent).toBe(heading);
			expect(host.textContent).toContain('Something went wrong. Try again, or save a report to send.');
			expect(host.textContent).toContain('Retry');
			expect(host.textContent).toContain('Use this computer instead');
		};
		await vi.waitFor(expectErrorActions);
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Retry'))!.click();
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
		await vi.waitFor(expectErrorActions);
	});
	it('shows the no-account state with the CLI command instead of a form', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(false)));
		mountScreen();

		await vi.waitFor(() => expect(host.textContent).toContain('corsia user add'));
		expect(usernameField()).toBeNull();
		expect(host.textContent).toContain('Check again');
	});

	it('notifies its host when no account exists', async () => {
		const onNoAccount = vi.fn();
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(false)));
		mountScreen({ onNoAccount });
		await vi.waitFor(() => expect(onNoAccount).toHaveBeenCalledOnce());
	});

	it('keeps the CLI guidance and never hands over to setup when setup refuses the client', async () => {
		const onNoAccount = vi.fn();
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/setup/status') return { ok: false, status: 403, json: async () => ({}) };
			return statusResponse(false);
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen({ onNoAccount });

		await vi.waitFor(() => expect(host.textContent).toContain('corsia user add'));
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/setup/status'));
		expect(onNoAccount).not.toHaveBeenCalled();
		expect(host.textContent).toContain('Check again');
	});

	it('re-checks status when Check again is clicked and shows the form once an account exists', async () => {
		const statuses = [statusResponse(false), statusResponse(true)];
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input) === '/api/setup/status') return { ok: false, status: 403, json: async () => ({}) };
			return statuses.shift();
		});
		vi.stubGlobal('fetch', fetchMock);
		mountScreen();
		await vi.waitFor(() => expect(host.textContent).toContain('Check again'));

		const checkAgain = Array.from(host.querySelectorAll('button'))
			.find((b) => b.textContent?.includes('Check again'));
		checkAgain!.click();
		flushSync();

		await vi.waitFor(() => expect(usernameField()).not.toBeNull());
		expect(fetchMock.mock.calls.filter(([url]) => url === '/api/auth/status')).toHaveLength(2);
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

	it('offers the configured computer return action only while connected remotely', async () => {
		remoteShellMocks.shellInfo.mockReturnValue({ mode: 'remote', name: 'Studio', localConfigured: true });
		remoteShellMocks.disconnectRemote.mockResolvedValue(undefined);
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(true)));
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		const returnButton = Array.from(host.querySelectorAll('button'))
			.find((button) => button.textContent?.includes('Use this computer instead'));
		expect(returnButton).toBeDefined();
		returnButton!.click();
		await vi.waitFor(() => expect(remoteShellMocks.disconnectRemote).toHaveBeenCalledOnce());
		expect(host.textContent).not.toContain('Disconnect from Studio?');
	});

	it('labels the remote return action as setup when this computer is not configured', async () => {
		remoteShellMocks.shellInfo.mockReturnValue({ mode: 'remote', name: 'Studio', localConfigured: false });
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(true)));
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		expect(host.textContent).toContain('Set up this computer instead');
		expect(host.textContent).not.toContain('Use this computer instead');
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Set up this computer instead'))!.click();
		await vi.waitFor(() => expect(remoteShellMocks.disconnectRemote).toHaveBeenCalledOnce());
		expect(host.textContent).not.toContain('Disconnect from Studio?');
	});

	it.each([true, false])('offers a way back while checking status only when remote: %s', async (remote) => {
		remoteShellMocks.shellInfo.mockReturnValue(remote ? { mode: 'remote', name: 'Studio', localConfigured: true } : null);
		vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
		mountScreen();
		const button = Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.includes('Use this computer instead'));
		expect(Boolean(button)).toBe(remote);
		if (button) {
			button.click();
			await vi.waitFor(() => expect(remoteShellMocks.disconnectRemote).toHaveBeenCalledOnce());
		}
	});

	it.each([true, false])('offers a way back on the no-account screen only when remote: %s', async (remote) => {
		remoteShellMocks.shellInfo.mockReturnValue(remote ? { mode: 'remote', name: 'Studio', localConfigured: true } : null);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input) === '/api/setup/status' ? { ok: false, status: 403 } : statusResponse(false)));
		mountScreen();
		await vi.waitFor(() => expect(host.textContent).toContain('corsia user add'));
		const button = Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.includes('Use this computer instead'));
		expect(Boolean(button)).toBe(remote);
		if (button) {
			button.click();
			await vi.waitFor(() => expect(remoteShellMocks.disconnectRemote).toHaveBeenCalledOnce());
		}
	});

	it.each([true, false])('offers a way back on the status-error screen only when remote: %s', async (remote) => {
		remoteShellMocks.shellInfo.mockReturnValue(remote ? { mode: 'remote', name: 'Studio', localConfigured: false } : null);
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('unavailable'); }));
		mountScreen();
		await vi.waitFor(() => expect(host.textContent).toContain(remote ? 'Can’t check Studio' : 'CORSIA couldn’t start'));
		const button = Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.includes('Set up this computer instead'));
		expect(Boolean(button)).toBe(remote);
		if (button) {
			button.click();
			await vi.waitFor(() => expect(remoteShellMocks.disconnectRemote).toHaveBeenCalledOnce());
		}
	});

	it('does not offer the remote return action outside the desktop remote shell', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => statusResponse(true)));
		mountScreen();
		await vi.waitFor(() => expect(usernameField()).not.toBeNull());

		expect(host.textContent).not.toContain('this computer instead');
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
