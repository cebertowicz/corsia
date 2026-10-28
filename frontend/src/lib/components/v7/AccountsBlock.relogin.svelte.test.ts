import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import AccountsBlock from './AccountsBlock.svelte';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';
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
function serve(accounts: VendorAccount[], signinResult: unknown = null) {
	const calls: string[] = [];
	vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
		const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
		const path = new URL(raw, 'http://test.local').pathname;
		const method = init?.method ?? 'GET';
		calls.push(`${method} ${path}`);
		if (path.startsWith('/api/provider-policy')) return Promise.resolve(response({ providers: [] }));
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
		resetSettingsNavigation();
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	it('puts Sign in again after Rename on a signed-in added account and starts the existing sign-in', async () => {
		const calls = serve([personal, signedIn], { account_id: 'fomrrt1', route: 'browser', status: 'waiting', cause: null });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('fomrrt1');
		expect(menuLabels()).toEqual(['Rename', 'Sign in again', 'Sign out', 'Remove']);
		document.querySelector<HTMLButtonElement>('[data-menu-id="sign-in-again"]')!.click();
		await vi.waitFor(() => expect(calls).toContain('POST /api/vendor-accounts/fomrrt1/signin'));
		// The vendor's answer was "not signed in": the sign-in runs on the existing status line.
		await vi.waitFor(() => expect(document.querySelector('#vendor-account-fomrrt1 .signin-state')?.textContent).toContain('Waiting for sign-in in your browser'));
		expect(document.querySelector('[role="menu"]')).toBeNull();
		unmount(component);
	});

	it('is on a signed-out added account too, beside its Sign in button', async () => {
		serve([personal, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('fomrrt2');
		expect(menuLabels()).toContain('Sign in again');
		unmount(component);
	});

	it('is not on the Default account or one the provider manages', async () => {
		const managed = account('managed', 'Managed', { state: 'legacy' });
		serve([personal, managed]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('personal');
		expect(menuLabels()).toEqual(['Rename', 'Sign out', 'Remove']);
		document.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click();
		await settle();
		await openMenu('managed');
		expect(menuLabels()).toEqual(['Rename']);
		unmount(component);
	});

	it('says so on the status line when the vendor still has the account signed in', async () => {
		serve([personal, signedIn], { account_id: 'fomrrt1', route: null, status: 'already_signed_in', cause: null });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('fomrrt1');
		document.querySelector<HTMLButtonElement>('[data-menu-id="sign-in-again"]')!.click();
		await vi.waitFor(() => expect(document.querySelector('#vendor-account-fomrrt1 .signin-state')?.textContent?.trim()).toBe('FOMRRT1 is still signed in. Its model list is up to date.'));
		expect(document.querySelector('#vendor-account-fomrrt1 .signin-state button')).toBeNull();
		unmount(component);
	});

	it('reports a failed model reload on the status line with its reference and no control of its own', async () => {
		serve([personal, signedIn], { account_id: 'fomrrt1', route: null, status: 'model_refresh_failed', cause: 'account_refresh_failed', ref: 'R-7K2M' });
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		await openMenu('fomrrt1');
		document.querySelector<HTMLButtonElement>('[data-menu-id="sign-in-again"]')!.click();
		await vi.waitFor(() => expect(document.querySelector('#vendor-account-fomrrt1 .signin-state')?.textContent?.trim()).toBe('FOMRRT1 is still signed in, but its model list couldn’t be reloaded. Use Sign in again to retry. Reference: R-7K2M'));
		expect(document.querySelector('#vendor-account-fomrrt1 .signin-state button')).toBeNull();
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
		resetSettingsNavigation();
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	it('focuses Sign in on a signed-out account and marks it as the link did elsewhere', async () => {
		serve([personal, signedIn, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		requestSettingsSection('providers', 'account-fomrrt2');
		await vi.waitFor(() => expect(document.querySelector('[data-chat-link-focus]')).not.toBeNull());
		const target = document.querySelector<HTMLButtonElement>('#vendor-account-fomrrt2 [data-chat-link-focus] .btn')!;
		expect(target.textContent?.trim()).toBe('Sign in');
		// Drawn like Sign in again, so the link lands on a control with a box of its own.
		expect(target.classList.contains('secondary')).toBe(true);
		expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
		expect(document.activeElement).toBe(target);
		expect(document.querySelectorAll('[data-chat-link-focus]')).toHaveLength(1);
		expect(get(settingsNavigation).anchor).toBeNull();
		// Leaving the button clears the mark.
		target.blur();
		await vi.waitFor(() => expect(document.querySelector('[data-chat-link-focus]')).toBeNull());
		unmount(component);
	});

	it('opens a signed-in account’s menu with Sign in again focused', async () => {
		serve([personal, signedIn, signedOut]);
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		requestSettingsSection('providers', 'account-fomrrt1');
		await vi.waitFor(() => expect(document.querySelector('[role="menu"]')).not.toBeNull());
		expect(document.activeElement?.getAttribute('data-menu-id')).toBe('sign-in-again');
		expect(menuLabels()).toEqual(['Rename', 'Sign in again', 'Sign out', 'Remove']);
		expect(get(settingsNavigation).anchor).toBeNull();
		// A menu the person opens themselves still starts on its first item.
		document.querySelector<HTMLButtonElement>('.menu-backdrop')!.click();
		await settle();
		await openMenu('fomrrt1');
		expect(document.activeElement?.getAttribute('data-menu-id')).toBe('rename');
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
