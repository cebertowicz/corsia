import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AppSignInPanel from './AppSignInPanel.svelte';
import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
import { token } from '$lib/stores/auth';
import { signInStateLine } from '$lib/appSignIn';
import type { ConnectedApp } from '$lib/api/connectedApps';

const reach = { mode: 'all' as const, project_ids: [], outside_projects: true };
const linear = (patch: Partial<ConnectedApp> = {}): ConnectedApp => ({
	id: 'linear', name: 'Linear', state: 'needs_sign_in', revision: 1, reach, sign_in: { status: 'idle', signed_in: false }, ...patch,
});
const LINK = 'https://auth.example.test/authorize?state=abc';
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('test-token');
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	token.set(null);
});

const button = (label: string): HTMLButtonElement | undefined => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
const reply = (body: unknown, ok = true) => ({ ok, status: ok ? 200 : 502, json: async () => body, clone() { return this; } });

function mountPanel(app: ConnectedApp, props: Record<string, unknown> = {}) {
	const onChange = vi.fn();
	const onRefresh = vi.fn();
	const onSignedOut = vi.fn();
	component = mount(AppSignInPanel, { target: host, props: { app, signInHere: true, confirmOut: false, onConfirmOut: vi.fn(), onChange, onRefresh, onSignedOut, ...props } });
	return { onChange, onRefresh, onSignedOut };
}

describe('sign-in row lines', () => {
	it('names each moment in the copy sheet words', () => {
		expect(signInStateLine(linear(), false)).toBe('Needs you to sign in');
		expect(signInStateLine(linear(), true)).toBe('Signed out');
		expect(signInStateLine(linear({ sign_in: { status: 'pending', signed_in: false } }), false)).toBe('Waiting for you in your browser');
		expect(signInStateLine(linear({ sign_in: { status: 'failed', signed_in: false, failure_class: 'declined' } }), false)).toBe("Sign-in didn't finish");
		expect(signInStateLine(linear({ state: 'working', tool_count: 12, sign_in: { status: 'idle', signed_in: true, account: 'steve@acme.example' } }), false))
			.toBe('Connected · 12 actions · Signed in as steve@acme.example');
		expect(signInStateLine(linear({ state: 'working', tool_count: 12, sign_in: { status: 'idle', signed_in: true } }), false)).toBe('Connected · 12 actions · Signed in');
		expect(signInStateLine({ ...linear(), state: 'working', sign_in: undefined }, false)).toBeNull();
	});
});

describe('AppSignInPanel', () => {
	it('opens a window inside the click, then points it at the link', async () => {
		const page = { opener: {}, location: { href: '' }, close: vi.fn() };
		const open = vi.fn(() => page);
		vi.stubGlobal('open', open);
		const fetch = vi.fn(async () => reply({ authorization_url: LINK, expires_at: 't', reopened: false }));
		vi.stubGlobal('fetch', fetch);
		const { onChange } = mountPanel(linear());
		button('Sign in')!.click();
		expect(open).toHaveBeenCalledTimes(1);
		expect(open).toHaveBeenCalledWith('', '_blank');
		await vi.waitFor(() => expect(page.location.href).toBe(LINK));
		expect(page.opener).toBeNull();
		expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/linear/sign-in'), { method: 'POST' });
		expect(onChange).toHaveBeenCalledWith({ sign_in: expect.objectContaining({ status: 'pending' }) });
		expect(host.textContent).not.toContain(LINK);
	});

	it('offers the link to copy when the browser blocks the window', async () => {
		vi.stubGlobal('open', vi.fn(() => null));
		vi.stubGlobal('fetch', vi.fn(async () => reply({ authorization_url: LINK, expires_at: 't', reopened: false })));
		const writeText = vi.fn(async () => {});
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		const view = $state({ app: linear() });
		component = mount(AppSignInPanel, { target: host, props: { get app() { return view.app; }, signInHere: true, onChange: (patch: Partial<ConnectedApp>) => { view.app = { ...view.app, ...patch }; }, onRefresh: vi.fn(), onSignedOut: vi.fn() } });
		button('Sign in')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain("Your browser didn't open."));
		expect(host.textContent).not.toContain(LINK);
		button('Copy sign-in link')!.click();
		await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith(LINK));
		await vi.waitFor(() => expect(button('Copied')).toBeTruthy());
	});

	it('shows the waiting words with Open the page again and Cancel, and cancel calls the route', async () => {
		const fetch = vi.fn(async () => reply({ cancelled: true }));
		vi.stubGlobal('fetch', fetch);
		const { onChange } = mountPanel(linear({ sign_in: { status: 'pending', signed_in: false } }));
		expect(host.textContent).toContain("Linear's sign-in page is open in your browser. Allow CORSIA there, then come back.");
		expect(button('Open the page again')!.classList.contains('ghost')).toBe(true);
		expect(button('Cancel')!.classList.contains('ghost')).toBe(true);
		button('Cancel')!.click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/sign-in'), { method: 'DELETE' }));
		await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith({ sign_in: expect.objectContaining({ status: 'idle' }) }));
	});

	it('asks the backend how a pending sign-in is going and refreshes when it ends', async () => {
		vi.useFakeTimers();
		try {
			vi.stubGlobal('fetch', vi.fn(async () => reply({ status: 'idle' })));
			const { onRefresh } = mountPanel(linear({ sign_in: { status: 'pending', signed_in: false } }));
			await vi.advanceTimersByTimeAsync(2100);
			expect(onRefresh).toHaveBeenCalled();
		} finally { vi.useRealTimers(); }
	});

	it.each([
		['timed_out', "Sign-in wasn't finished within 5 minutes, or the tab was closed."],
		['declined', "The sign-in was declined on Linear's page."],
		['not_allowed', "Linear doesn't allow CORSIA to sign in."],
		['permissions', "Linear can't give CORSIA the access it needs."],
		['app_error', 'Linear had a problem with the sign-in.'],
		['app_busy', 'Linear is busy.'],
		['address_mismatch', "Linear didn't recognize CORSIA. It needs a code called a client ID."],
	])('names the %s failure in plain words', (failureClass, text) => {
		mountPanel(linear({ sign_in: { status: 'failed', signed_in: false, failure_class: failureClass } }));
		expect(host.textContent).toContain(text);
		const labels: Record<string, string | null> = { permissions: null, not_allowed: null, address_mismatch: 'Sign in' };
		const label = failureClass in labels ? labels[failureClass] : 'Try again';
		if (label) expect(button(label)!.classList.contains('primary')).toBe(true);
		else expect(host.querySelectorAll('.actions .btn')).toHaveLength(0);
	});

	it('says Remove is the way out when the app will not allow CORSIA', () => {
		mountPanel(linear({ sign_in: { status: 'failed', signed_in: false, failure_class: 'not_allowed' } }));
		expect(host.textContent).toContain('or press Remove to take it off this list.');
	});

	it('offers Sign in, not Try again, while the app has no client ID, and starts a fresh sign-in', async () => {
		vi.stubGlobal('open', vi.fn(() => null));
		const fetch = vi.fn(async () => reply({ authorization_url: LINK, expires_at: 't', reopened: false }));
		vi.stubGlobal('fetch', fetch);
		const { onChange } = mountPanel(linear({ sign_in: { status: 'failed', signed_in: false, failure_class: 'address_mismatch' } }));
		expect(button('Try again')).toBeUndefined();
		button('Sign in')!.click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/linear/sign-in'), { method: 'POST' }));
		await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith({ sign_in: expect.objectContaining({ status: 'pending', failure_class: undefined }) }));
	});

	it("shows the app's own words only for an unknown failure", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => reply({ status: 'failed', failure_class: 'unknown', words: 'tenant is frozen' })));
		mountPanel(linear({ sign_in: { status: 'failed', signed_in: false, failure_class: 'unknown' } }));
		await vi.waitFor(() => expect(host.textContent).toContain('What Linear said'));
		expect(host.textContent).toContain('tenant is frozen');
	});

	it('does not offer Sign in away from the CORSIA Mac', () => {
		mountPanel(linear(), { signInHere: false });
		expect(host.textContent).toContain('You can only sign in on the Mac CORSIA runs on. Open CORSIA there and press Sign in.');
		expect(button('Sign in')).toBeUndefined();
	});

	it('signs out once the confirm is showing, and says when the provider was not told', async () => {
		const fetch = vi.fn(async () => reply({ id: 'linear', had_sign_in: true, provider_told: false, provider_url: 'https://linear.example.test', state: 'needs_sign_in' }));
		vi.stubGlobal('fetch', fetch);
		const { onSignedOut, onChange } = mountPanel(linear({ state: 'working', sign_in: { status: 'idle', signed_in: true, account: 'steve@acme.example' } }), { confirmOut: true });
		expect(host.textContent).toContain('Sign out of Linear?');
		expect(host.textContent).toContain('You can sign in again any time.');
		expect(fetch).not.toHaveBeenCalled();
		button('Sign out')!.click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/linear/sign-out'), { method: 'POST' }));
		await vi.waitFor(() => expect(onSignedOut).toHaveBeenCalled());
		expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ state: 'needs_sign_in' }));
		await vi.waitFor(() => expect(host.textContent).toContain("Linear may still list CORSIA as allowed."));
		expect((host.querySelector('a') as HTMLAnchorElement).href).toBe('https://linear.example.test/');
	});
});

describe('ConnectedAppsSettings with sign-in', () => {
	it('opens a row that needs sign-in, shows the lede once, and the Sign in action', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => reply({ servers: [linear()], sign_in_here: true })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Needs you to sign in'));
		expect(host.textContent!.split("so CORSIA never sees your password.").length - 1).toBe(1);
		expect(button('Sign in')!.classList.contains('primary')).toBe(true);
	});

	it('hides Sign in when the backend says the person is not on its Mac', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => reply({ servers: [linear()], sign_in_here: false })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('You can only sign in on the Mac CORSIA runs on.'));
		expect(button('Sign in')).toBeUndefined();
	});

	it.each([
		['open while the app is not signed in', false, 'true'],
		['collapsed once the app is signed in', true, 'false'],
	])('keeps a failed row %s', async (_name, signedIn, expanded) => {
		const app = linear({ state: signedIn ? 'working' : 'needs_sign_in', sign_in: { status: 'failed', signed_in: signedIn, failure_class: 'address_mismatch' } });
		vi.stubGlobal('fetch', vi.fn(async () => reply({ servers: [app], sign_in_here: true })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain("Sign-in didn't finish"));
		const summary = host.querySelector('button.face.open') as HTMLButtonElement;
		expect(summary.getAttribute('aria-expanded')).toBe(expanded);
		expect(Boolean(button('Sign in'))).toBe(!signedIn);
		if (signedIn) {
			summary.click();
			flushSync();
			summary.click();
			flushSync();
			expect(summary.getAttribute('aria-expanded'), 'the person can close it again').toBe('false');
		}
	});

	it('shows a signed-in account on the row', async () => {
		const app = linear({ state: 'working', tool_count: 3, sign_in: { status: 'idle', signed_in: true, account: 'steve@acme.example' } });
		vi.stubGlobal('fetch', vi.fn(async () => reply({ servers: [app], sign_in_here: true })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Connected · 3 actions · Signed in as steve@acme.example'));
	});

	it('puts Sign out after Reconnect and before Remove, and asks before signing out', async () => {
		const app = linear({ state: 'working', tool_count: 3, sign_in: { status: 'idle', signed_in: true, account: 'steve@acme.example' } });
		vi.stubGlobal('fetch', vi.fn(async () => reply({ servers: [app], sign_in_here: true })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Signed in as steve@acme.example'));
		(host.querySelector('button.face.open') as HTMLButtonElement).click();
		flushSync();
		const labels = [...host.querySelectorAll('.actions button')].map((b) => b.textContent?.trim());
		expect(labels).toEqual(['Where it works…', 'Reconnect', 'Sign out', 'Remove']);
		button('Sign out')!.click();
		flushSync();
		expect(host.textContent).toContain('Sign out of Linear?');
		expect([...host.querySelectorAll('button')].filter((b) => b.textContent?.trim() === 'Sign out')).toHaveLength(1);
	});
});
