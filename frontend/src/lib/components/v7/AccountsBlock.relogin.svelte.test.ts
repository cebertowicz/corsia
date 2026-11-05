import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import AccountsBlock from './AccountsBlock.svelte';
import { vendorAccounts, signin, type VendorAccount } from '$lib/stores/vendorAccounts';
import { token } from '$lib/stores/auth';
import { requestSettingsSection, resetSettingsNavigation, settingsNavigation } from '$lib/stores/settingsNavigation';

const provider = {
	name: 'claude-code', label: 'Claude Code', type: 'harness' as const, statusDot: 'online' as const,
	subline: '3 models', raw: { name: 'claude-code', label: 'Claude Code', type: 'harness' as const, has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] },
};

const account = (id: string, name: string, patch: Partial<VendorAccount> = {}): VendorAccount => ({
	id, provider: 'claude-code', name, is_default: false, state: 'signed_in', revision: 1, identity: {},
	label: { name, email: null, text: name, custom: name }, ...patch,
});

const personal = account('personal', 'Personal', { is_default: true });
const signedIn = account('fomrrt1', 'FOMRRT1');
const signedOut = account('fomrrt2', 'FOMRRT2', { state: 'signed_out' });

function response(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** The accounts list, an empty policy, and `signinResult` for a POST to /signin; every call is recorded. */
function serve(accounts: VendorAccount[], signinResult: unknown = null, verifyResult: unknown = { state: 'signed_in', vendor_signed_in: true, model_refresh_ok: true, model_refresh_cause: null, model_refresh_ref: null }) {
	const calls: string[] = [];
	vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
		const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
		const path = new URL(raw, 'http://test.local').pathname;
		const method = init?.method ?? 'GET';
		calls.push(`${method} ${path}`);
		if (path.startsWith('/api/provider-policy')) return Promise.resolve(response({ providers: [] }));
		if (path.endsWith('/verify')) return Promise.resolve(response(verifyResult));
		if (path.endsWith('/signin/check')) return Promise.resolve(response({ detail: 'no sign-in is in progress for this account' }, 404));
		if (path.endsWith('/signin') && method === 'POST') return Promise.resolve(response(signinResult));
		return Promise.resolve(response({ accounts }));
	}));
	return calls;
}

async function settle(): Promise<void> {
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

async function openMenu(id: string): Promise<void> {
	document.getElementById(`vendor-account-${id}`)!.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 8, clientY: 8 }));
	await settle();
}

const menuLabels = () => [...document.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent?.trim());

describe('Sign in again on an account row', () => {
	afterEach(() => {
		token.set(null);
		vendorAccounts.set([]);
		signin.set({});
		resetSettingsNavigation();
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	it('offers Rename, Sign out and Remove on a signed-in account, and no Sign in again', async () => {
		serve([personal, signedIn]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('fomrrt1');
		expect(menuLabels()).toEqual(['Rename', 'Sign out', 'Remove']);
		expect(document.querySelector('[data-menu-id="sign-in-again"]')).toBeNull();
		unmount(component);
	});

	it('offers Rename and Remove on a signed-out account, whose Sign in is on its row', async () => {
		serve([personal, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('fomrrt2');
		expect(menuLabels()).toEqual(['Rename', 'Remove']);
		expect(document.querySelector('#vendor-account-fomrrt2 .account-signin-action')?.textContent?.trim()).toBe('Sign in');
		unmount(component);
	});

	it('offers the Default the same menu as any signed-in account', async () => {
		serve([personal, signedIn]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('personal');
		expect(menuLabels()).toEqual(['Rename', 'Sign out', 'Remove']);
		unmount(component);
	});

	it('says so on the status line when the vendor still has the account signed in', async () => {
		serve([personal, signedIn]);
		signin.set({ fomrrt1: { account_id: 'fomrrt1', route: null, status: 'already_signed_in', cause: null } });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		expect(document.querySelector('#vendor-account-fomrrt1 .signin-state')?.textContent?.trim()).toBe('FOMRRT1 is still signed in. Its model list is up to date.');
		expect(document.querySelector('#vendor-account-fomrrt1 .signin-state button')).toBeNull();
		unmount(component);
	});

	it('offers Refresh models on a failed model reload, and a good retry replaces the line', async () => {
		const calls = serve([personal, signedIn]);
		signin.set({ fomrrt1: { account_id: 'fomrrt1', route: null, status: 'model_refresh_failed', cause: null, ref: 'R-7K2M' } });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		const row = '#vendor-account-fomrrt1 .signin-state';
		expect(document.querySelector(row)?.textContent).toContain('FOMRRT1 is still signed in, but its model list couldn’t be reloaded. Use Refresh models to retry. Reference: R-7K2M');
		const button = document.querySelector<HTMLButtonElement>(row + ' button')!;
		expect(button.textContent?.trim()).toBe('Refresh models');
		button.click();
		flushSync();
		expect(button.textContent?.trim()).toBe('Refreshing…');
		expect(button.disabled).toBe(true);
		await vi.waitFor(() => expect(calls).toContain('POST /api/vendor-accounts/fomrrt1/verify'));
		await vi.waitFor(() => expect(document.querySelector(row)?.textContent?.trim()).toContain('FOMRRT1 is still signed in. Its model list is up to date.'));
		expect(document.querySelector(row + ' button')).toBeNull();
		unmount(component);
	});

	it('does not show an error when a model refresh was superseded', async () => {
		serve([personal, signedIn], null, { state: 'signed_in', vendor_signed_in: true, model_refresh_ok: null, model_refresh_cause: 'account_refresh_superseded', model_refresh_ref: null });
		signin.set({ fomrrt1: { account_id: 'fomrrt1', route: null, status: 'model_refresh_failed', cause: null, ref: 'R-7K2M' } });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		document.querySelector<HTMLButtonElement>('#vendor-account-fomrrt1 .signin-state button')!.click();
		await settle();
		expect(document.querySelector('#vendor-account-fomrrt1 .signin-state')).toBeNull();
		unmount(component);
	});
});

describe('Sign in on a signed-out account row', () => {
	afterEach(() => {
		token.set(null);
		vendorAccounts.set([]);
		signin.set({});
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	const signInButtons = () => [...document.querySelectorAll<HTMLButtonElement>('#vendor-account-fomrrt2 button')].filter((button) => /^Sign in( again)?$/.test(button.textContent?.trim() ?? ''));

	it('sits in the account row beside the name, in the row’s controls slot', async () => {
		serve([personal, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		const button = signInButtons()[0];
		expect(button.textContent?.trim()).toBe('Sign in');
		expect(button.closest('.account-usage-row .controls')).not.toBeNull();
		// "Signed out" stays under the name.
		expect(document.querySelector('#vendor-account-fomrrt2 .account-meta')?.textContent?.trim()).toBe('Signed out');
		unmount(component);
	});

	const results: [string, Record<string, unknown>, number, string | null][] = [
		['failed', { status: 'failed', cause: 'vendor_login_failed' }, 1, 'Sign in again'],
		['refused', { status: 'refused', cause: null, held_by: null }, 1, 'Sign in'],
		['waiting', { status: 'waiting', cause: null }, 0, null],
		['completed', { status: 'completed', cause: null }, 0, null],
	];
	it.each(results)('never shows two sign-in buttons on one row once sign-in is %s', async (_name, result, count, label) => {
		serve([personal, signedOut], { account_id: 'fomrrt2', route: 'browser', ...result });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		expect(signInButtons().map((button) => button.textContent?.trim())).toEqual(['Sign in']);
		signInButtons()[0].click();
		await vi.waitFor(() => expect(document.querySelector('#vendor-account-fomrrt2 .signin-state')).not.toBeNull());
		await settle();
		expect(signInButtons().map((button) => button.textContent?.trim())).toEqual(label ? [label] : []);
		expect(signInButtons()).toHaveLength(count);
		// After a failure the slot beside the name is empty: the button is with the cause on the status line.
		if (_name === 'failed') expect(document.querySelector('#vendor-account-fomrrt2 .account-usage-row .link-target')).toBeNull();
		unmount(component);
	});
});

describe('a settings link to one account', () => {
	const originalScroll = Element.prototype.scrollIntoView;
	beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
	afterEach(() => {
		Element.prototype.scrollIntoView = originalScroll;
		token.set(null);
		vendorAccounts.set([]);
		signin.set({});
		resetSettingsNavigation();
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	it('focuses Sign in on a signed-out account', async () => {
		serve([personal, signedIn, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		requestSettingsSection('providers', 'account-fomrrt2');
		const target = (): HTMLButtonElement | null => document.querySelector<HTMLButtonElement>('#vendor-account-fomrrt2 [data-link-target] button');
		await vi.waitFor(() => expect(document.activeElement).toBe(target()));
		expect(target()?.textContent?.trim()).toBe('Sign in');
		expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
		expect(get(settingsNavigation).anchor).toBeNull();
		unmount(component);
	});

	it('only scrolls to and focuses a signed-in account row, and opens no menu', async () => {
		serve([personal, signedIn, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		requestSettingsSection('providers', 'account-fomrrt1');
		await vi.waitFor(() => expect(document.activeElement?.id).toBe('vendor-account-fomrrt1'));
		expect(document.querySelector('[role="menu"]')).toBeNull();
		expect(get(settingsNavigation).anchor).toBeNull();
		// A menu the person opens themselves starts on its first item.
		await openMenu('fomrrt1');
		expect(document.activeElement?.getAttribute('data-menu-id')).toBe('rename');
		expect(menuLabels()).toEqual(['Rename', 'Sign out', 'Remove']);
		unmount(component);
	});

	it('only scrolls to and focuses the Default account, which has nothing to sign in again', async () => {
		serve([personal, signedIn]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		requestSettingsSection('providers', 'account-personal');
		await vi.waitFor(() => expect(document.activeElement?.id).toBe('vendor-account-personal'));
		expect(document.querySelector('[role="menu"]')).toBeNull();
		unmount(component);
	});
});
