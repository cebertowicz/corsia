import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import SystemMessageRow from './SystemMessageRow.svelte';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';

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

	it('shows a lost-login error as written and links Settings › Providers to the account named', () => {
		const text = 'FOMRRT1 is signed out of Claude Code. Sign it in again under Settings › Providers.';
		vendorAccounts.set([account('fomrrt1')]);
		render({ id: 'lost-login', role: 'system', content: text, metadata: { type: 'result', cause: 'sign_in_expired', cc: true, account_id: 'fomrrt1' } });

		expect(host.textContent?.trim()).toBe(text);
		expect(host.querySelector('details')).toBeNull();
		const link = host.querySelector<HTMLAnchorElement>('a')!;
		expect(link.textContent).toBe('Settings › Providers');
		expect(link.getAttribute('href')).toBe('/settings/providers#account-fomrrt1');
		expect(link.closest('.md-content')).not.toBeNull();
	});

	it('links the same way in any error that names an account', () => {
		const text = 'Couldn’t start Codex on FOMRRT1: its model list is out of date. Use Sign in again on FOMRRT1 under Settings › Providers.';
		vendorAccounts.set([account('fomrrt1', { state: 'signed_in' })]);
		render({ id: 'stale-models', role: 'system', content: text, metadata: { type: 'harness_error', account_id: 'fomrrt1' } });

		expect(host.textContent?.trim()).toBe(text);
		expect(host.querySelector('a')?.getAttribute('href')).toBe('/settings/providers#account-fomrrt1');
	});

	it('keeps the text for the Default and legacy accounts, which have no row to press', () => {
		vendorAccounts.set([account('personal', { is_default: true, state: 'signed_in' }), account('older', { state: 'legacy' }), account('gone', { state: 'removed' })]);
		for (const id of ['personal', 'older', 'gone']) {
			render({ id: `plain-${id}`, role: 'system', content: 'Sign it in again under Settings › Providers.', metadata: { type: 'harness_error', account_id: id } });
		}

		expect(host.querySelector('a')).toBeNull();
		expect(host.textContent).toContain('Sign it in again under Settings › Providers.');
	});

	it('writes no link without a usable account id to land on', () => {
		render({ id: 'no-account', role: 'system', content: 'Sign it in again under Settings › Providers.', metadata: { type: 'harness_error' } });
		render({ id: 'odd-account', role: 'system', content: 'Sign it in again under Settings › Providers.', metadata: { type: 'harness_error', account_id: 'x y' } });

		expect(host.querySelector('a')).toBeNull();
	});
});
