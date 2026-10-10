import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import AccountBillingRow from './AccountBillingRow.svelte';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';

const account = (over: Partial<VendorAccount> = {}): VendorAccount => ({
	id: 'work', provider: 'codex', name: 'Work', is_default: false, state: 'signed_in', revision: 1, identity: {},
	label: { name: 'Work', email: null, text: 'Work', custom: null }, billing: null, billing_revision: 4, ...over,
});
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const button = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === name);
const text = () => document.body.textContent?.replace(/\s+/g, ' ').trim() ?? '';
async function settle() { await new Promise((resolve) => setTimeout(resolve, 0)); flushSync(); }
function type(element: Element | null, value: string) {
	const input = element as HTMLInputElement | HTMLSelectElement;
	input.value = value;
	input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
	flushSync();
}
let component: ReturnType<typeof mount> | null = null;
function show(row: VendorAccount) {
	vendorAccounts.set([row]);
	component = mount(AccountBillingRow, { target: document.body, props: { account: row } });
	flushSync();
}
afterEach(() => { if (component) unmount(component); component = null; vendorAccounts.set([]); vi.unstubAllGlobals(); document.body.innerHTML = ''; });

describe('AccountBillingRow', () => {
	it('saves the price through the account route and shows the returned account', async () => {
		const fetch = vi.fn().mockResolvedValueOnce(reply(account({ billing: { monthly_price_cents: 2000, charge_day: 14 }, billing_revision: 5 })));
		vi.stubGlobal('fetch', fetch);
		show(account());
		button('Set price')!.click(); flushSync();
		type(document.querySelector('input[inputmode="decimal"]'), '20'); type(document.querySelector('select'), '14');
		button('Save')!.click(); await settle();
		expect(fetch.mock.calls[0][0]).toContain('/api/vendor-accounts/work/billing');
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ monthly_price_cents: 2000, charge_day: 14, expected_billing_revision: 4 });
		expect(get(vendorAccounts)[0]).toMatchObject({ billing: { monthly_price_cents: 2000, charge_day: 14 }, billing_revision: 5 });
		expect(text()).not.toContain('Save');
	});

	it('shows the stale text on 409 and Reload re-reads the accounts', async () => {
		const fetch = vi.fn()
			.mockResolvedValueOnce(reply({ detail: 'This account changed. Reload and try again.', code: 'billing_changed' }, 409))
			.mockResolvedValueOnce(reply({ accounts: [account({ billing: { monthly_price_cents: 900, charge_day: 2 }, billing_revision: 9 })] }));
		vi.stubGlobal('fetch', fetch);
		show(account());
		button('Set price')!.click(); flushSync();
		type(document.querySelector('input[inputmode="decimal"]'), '20'); type(document.querySelector('select'), '14');
		button('Save')!.click(); await settle();
		expect(text()).toContain('This account changed. Reload and try again.');
		button('Reload')!.click(); await settle();
		expect(fetch.mock.calls[1][0]).toContain('/api/vendor-accounts');
		expect(get(vendorAccounts)[0].billing_revision).toBe(9);
	});

	it('records overage for a month and confirms it', async () => {
		const fetch = vi.fn().mockResolvedValueOnce(reply(account({ billing_revision: 5 })));
		vi.stubGlobal('fetch', fetch);
		show(account());
		button('Add overage')!.click(); flushSync();
		type(document.querySelector('input[inputmode="decimal"]'), '12.50'); type(document.querySelector('select[aria-label^="Billing month"]'), '09'); type(document.querySelector('select[aria-label^="Billing year"]'), '2026');
		button('Save')!.click(); await settle();
		expect(fetch.mock.calls[0][0]).toContain('/api/vendor-accounts/work/overage');
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ amount_cents: 1250, month: '2026-09', expected_billing_revision: 4 });
		expect(text()).toContain('Overage $12.50 · Sep 2026 recorded.');
	});

	it('chooses the overage month with month and year selects, not a month input', () => {
		show(account());
		button('Add overage')!.click(); flushSync();
		expect(document.querySelector('input[type="month"]')).toBeNull();
		expect(document.querySelectorAll('select[aria-label^="Billing m"], select[aria-label^="Billing y"]')).toHaveLength(2);
		expect(document.querySelector('select[aria-label^="Billing month"]')!.textContent).toContain('September');
	});

	it('labels the overage month "Billing month" and names both selects, keeping the Month and Year placeholders', () => {
		show(account());
		button('Add overage')!.click(); flushSync();
		expect(text()).toContain('Billing month');
		expect(document.querySelector('select[aria-label="Billing month for Work"]')!.querySelector('option')!.textContent).toBe('Month');
		expect(document.querySelector('select[aria-label="Billing year for Work"]')!.querySelector('option')!.textContent).toBe('Year');
	});

	it('says a second save for the same month replaces the first', () => {
		show(account());
		expect(text()).not.toContain('replaces it');
		button('Add overage')!.click(); flushSync();
		expect(text()).toContain('Saving again for the same month replaces it.');
	});

	it('offers Add overage as a secondary button, like Set price', () => {
		show(account());
		expect(button('Add overage')!.className).toBe(button('Set price')!.className);
	});

	it('shows the server text when an account price save fails for any reason and keeps the form open', async () => {
		const fetch = vi.fn().mockResolvedValueOnce(reply({ detail: 'Could not save billing.' }, 500));
		vi.stubGlobal('fetch', fetch);
		show(account());
		button('Set price')!.click(); flushSync();
		type(document.querySelector('input[inputmode="decimal"]'), '20'); type(document.querySelector('select'), '14');
		button('Save')!.click(); await settle();
		expect(text()).toContain('Could not save billing.');
		expect(button('Reload')).toBeUndefined();
		expect(document.querySelector('input[inputmode="decimal"]')).not.toBeNull();
		expect(get(vendorAccounts)[0].billing).toBeNull();
	});

	it('shows no confirmation when an overage save fails', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(reply({ detail: 'Could not save overage.' }, 500)));
		show(account());
		button('Add overage')!.click(); flushSync();
		type(document.querySelector('input[inputmode="decimal"]'), '12.50'); type(document.querySelector('select[aria-label^="Billing month"]'), '09'); type(document.querySelector('select[aria-label^="Billing year"]'), '2026');
		button('Save')!.click(); await settle();
		expect(text()).toContain('Could not save overage.');
		expect(text()).not.toContain('recorded.');
	});

	it('validates the overage amount and month before sending', async () => {
		const fetch = vi.fn();
		vi.stubGlobal('fetch', fetch);
		show(account());
		button('Add overage')!.click(); flushSync();
		type(document.querySelector('input[inputmode="decimal"]'), 'abc');
		button('Save')!.click(); await settle();
		expect(text()).toContain('Enter a price of $0 or more with up to two decimals.');
		expect(text()).toContain('Choose a month and year.');
		expect(fetch).not.toHaveBeenCalled();
	});

	it('keeps a removed account read-only, with no overage action', () => {
		show(account({ state: 'removed', billing: { monthly_price_cents: 1500, charge_day: 3 } }));
		expect(text()).toContain('$15.00 a month · charged on the 3rd');
		expect(document.querySelectorAll('button')).toHaveLength(0);
	});

	it('leaves a signed-out account editable', () => {
		show(account({ state: 'signed_out' }));
		expect(button('Set price')).toBeTruthy();
	});
});
