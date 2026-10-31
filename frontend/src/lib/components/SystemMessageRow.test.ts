import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import SystemMessageRow from './SystemMessageRow.svelte';
import { signin, vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';

const account = (id: string, extra: Partial<VendorAccount> = {}): VendorAccount => ({
	id, provider: 'claude-code', name: id, is_default: false, state: 'signed_out', revision: 1, identity: {},
	label: { name: id, email: null, text: id, custom: id }, ...extra,
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	vendorAccounts.set([]);
	signin.set({});
	vi.unstubAllGlobals();
	if (component) unmount(component);
	component = null;
	host.remove();
});

function render(msg: unknown): void {
	component = mount(SystemMessageRow, { target: host, props: { msg } });
	flushSync();
}

describe('SystemMessageRow', () => {
	it('loads accounts once for several rows even when the response is empty', async () => {
		const fetchAccounts = vi.fn().mockResolvedValue(new Response(JSON.stringify({ accounts: [] }), { status: 200 }));
		vi.stubGlobal('fetch', fetchAccounts);
		const rows = ['first', 'second', 'third'].map((id) => mount(SystemMessageRow, {
			target: host, props: { msg: { role: 'system', content: 'Settings › Providers', metadata: { account_id: id } } },
		}));
		flushSync();
		await vi.waitFor(() => expect(fetchAccounts).toHaveBeenCalledTimes(1));
		await new Promise((resolve) => setTimeout(resolve, 0));
		flushSync();
		expect(fetchAccounts).toHaveBeenCalledTimes(1);
		for (const row of rows) await unmount(row);
	});

	it('puts the expired-Claude recovery line before raw details', () => {
		render({
			id: 'auth-failure', role: 'system',
			content: 'Failed to authenticate: OAuth session expired and could not be refreshed.',
			metadata: { type: 'result', cause: 'sign_in_expired', cc: true },
		});

		expect(host.textContent).toContain("Claude's sign-in has expired. Sign in again from the model card.");
		const details = host.querySelector('details');
		expect(details?.querySelector('summary')?.textContent).toBe('Show details');
		expect(details?.textContent).toContain('Failed to authenticate: OAuth session expired and could not be refreshed.');
	});

	it('offers sign-in in the expired account’s chat line', () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'no sign-in is in progress for this account' }), { status: 409 })));
		vendorAccounts.set([account('fomrrt1')]);
		render({ id: 'lost-login', role: 'system', content: 'OAuth session expired', metadata: { type: 'result', cause: 'sign_in_expired', cc: true, account_id: 'fomrrt1' } });
		expect(host.textContent).toContain('fomrrt1 is signed out of Claude Code.');
		expect(host.querySelector('button')?.textContent).toBe('Sign in');
		expect(host.querySelector('a')).toBeNull();
	});

	it('keeps Sign in available when another account holds sign-in', () => {
		vendorAccounts.set([account('personal'), account('work')]);
		signin.set({ personal: { account_id: 'personal', route: 'browser', status: 'refused', cause: null, held_by: 'work' } });
		render({ role: 'system', content: 'Personal is signed out. Settings › Providers', metadata: { type: 'harness_error', account_id: 'personal' } });
		expect(host.textContent).toContain('work is already signing in');
		expect(host.querySelector('button')?.textContent).toBe('Sign in');
	});

	it.each([
		['waiting', null, 'Waiting for sign-in in your browser.', false],
		['cancelled', null, 'Sign-in cancelled.', false],
		['refused', null, 'work is already signing in.', false],
		['failed', 'vendor_login_failed', 'Sign-in didn’t complete.', true],
		['failed', 'same_account', 'Already signed in as work.', true],
		['unavailable', null, 'Sign-in is unavailable.', true],
	] as const)('punctuates %s status and uses the appropriate treatment', (status, cause, copy, failure) => {
		vendorAccounts.set([account('personal'), account('work')]);
		signin.set({ personal: { account_id: 'personal', route: 'browser', status, cause, held_by: 'work', duplicate_of: 'work' } });
		render({ role: 'system', content: 'Personal is signed out. Settings › Providers', metadata: { type: 'harness_error', account_id: 'personal' } });
		const line = host.querySelector('.account-recovery')!;
		expect(line.textContent).toContain(copy);
		expect(line.classList.contains('recovery-failure')).toBe(failure);
		if (status === 'waiting') expect(line.querySelector('.recovery-actions')?.querySelectorAll('button')).toHaveLength(2);
	});

	it('offers model refresh in the stale-model chat line', () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'no sign-in is in progress for this account' }), { status: 409 })));
		vendorAccounts.set([account('fomrrt1', { provider: 'codex', state: 'signed_in' })]);
		render({ id: 'stale-models', role: 'system', content: 'model list out of date', metadata: { type: 'harness_error', cause: 'account_model_cache_changed', account_id: 'fomrrt1' } });
		expect(host.textContent).toContain('model list is out of date');
		expect(host.querySelector('button')?.textContent).toBe('Refresh models');
		expect(host.querySelector('a')).toBeNull();
	});

	it('shares a sign-in started in one chat with another chat for the same account', async () => {
		vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => Promise.resolve(
			url.includes('/signin/check')
				? new Response(JSON.stringify({ detail: 'no sign-in is in progress for this account' }), { status: 409 })
				: new Response(JSON.stringify({ account_id: 'personal', route: 'browser', status: 'waiting', cause: null }), { status: 200 }),
		)));
		vendorAccounts.set([account('personal')]);
		const message = { role: 'system', content: 'Personal is signed out. Settings › Providers', metadata: { type: 'harness_error', account_id: 'personal' } };
		const first = mount(SystemMessageRow, { target: host, props: { msg: message } });
		const second = mount(SystemMessageRow, { target: host, props: { msg: message } });
		flushSync();
		await vi.waitFor(() => expect((host.querySelector('button') as HTMLButtonElement).disabled).toBe(false));
		(host.querySelector('button') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelectorAll('.recovery-working')).toHaveLength(2));
		expect(host.querySelectorAll('.account-recovery button')).toHaveLength(4);
		await unmount(first); await unmount(second);
	});

	it('writes no link without a usable account id to land on', () => {
		render({ id: 'no-account', role: 'system', content: 'Sign it in again under Settings › Providers.', metadata: { type: 'harness_error' } });
		render({ id: 'odd-account', role: 'system', content: 'Sign it in again under Settings › Providers.', metadata: { type: 'harness_error', account_id: 'x y' } });

		expect(host.querySelector('a')).toBeNull();
	});
});
