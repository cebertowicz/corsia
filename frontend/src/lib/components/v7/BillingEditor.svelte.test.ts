import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BillingEditor from './BillingEditor.svelte';
import { billingSummary, ordinal, parseChargeDay, parsePriceCents } from './billingFormat';

type Save = (next: { mode?: 'subscription' | 'pay_per_use' | null; monthly_price_cents: number | null; charge_day: number | null }) => Promise<{ ok: boolean; error?: string; conflict?: boolean }>;

let component: ReturnType<typeof mount> | null = null;
afterEach(() => { if (component) unmount(component); component = null; document.body.innerHTML = ''; });

const set = { monthly_price_cents: 2000, charge_day: 14 };
const button = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === name);
const text = () => document.body.textContent?.replace(/\s+/g, ' ').trim() ?? '';
async function settle() { await new Promise((resolve) => setTimeout(resolve, 0)); flushSync(); }
function type(element: Element | null, value: string) {
	const input = element as HTMLInputElement | HTMLSelectElement;
	input.value = value;
	input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
	flushSync();
}
const price = () => document.querySelector('input[inputmode="decimal"]');
const day = () => document.querySelector('select');
function show(props: Record<string, unknown>) {
	const onsave = vi.fn<Save>(async () => ({ ok: true }));
	component = mount(BillingEditor, { target: document.body, props: { label: 'Work', billing: null, onsave, ...props } as never });
	flushSync();
	return onsave;
}

describe('billing formatting', () => {
	it('formats the collapsed copy for each state', () => {
		expect(billingSummary(set, null).text).toBe('$20.00 a month · charged on the 14th');
		expect(billingSummary({ monthly_price_cents: 0, charge_day: 1 }, null).text).toBe('$0.00 a month · charged on the 1st');
		expect(billingSummary(null, null).text).toBe('Price not recorded');
		expect(billingSummary(null, { mode: null, listPriced: true }).text).toBe('Price not recorded');
		expect(billingSummary(null, { mode: 'pay_per_use', listPriced: true })).toEqual({ text: 'Pay per use · Counted at list price' });
		expect(billingSummary(null, { mode: 'pay_per_use', listPriced: false })).toEqual({ text: 'Pay per use · No list price for this host' });
	});
	it('writes ordinals and parses only valid prices and days', () => {
		expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 31].map(ordinal)).toEqual(['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '31st']);
		expect([parsePriceCents('20'), parsePriceCents('0'), parsePriceCents('19.9'), parsePriceCents('$1,000'), parsePriceCents('100000.00')]).toEqual([2000, 0, 1990, null, 10_000_000]);
		expect(['', '-1', '1.234', 'abc', '100000.01', '1e3'].map(parsePriceCents)).toEqual([null, null, null, null, null, null]);
		expect(['', '0', '32', '1.5', '7'].map(parseChargeDay)).toEqual([null, null, null, null, 7]);
	});
});

describe('BillingEditor on an account', () => {
	it('reads a recorded price collapsed with Edit, and $0 as recorded', () => {
		show({ billing: set });
		expect(text()).toContain('Billing $20.00 a month · charged on the 14th');
		expect(button('Edit')).toBeTruthy();
		expect(button('Set price')).toBeUndefined();
		unmount(component!); document.body.innerHTML = '';
		component = mount(BillingEditor, { target: document.body, props: { label: 'Work', billing: { monthly_price_cents: 0, charge_day: 3 }, onsave: vi.fn() } as never });
		flushSync();
		expect(text()).toContain('$0.00 a month · charged on the 3rd');
	});

	it('reads an unset price with Set price and no Clear', () => {
		show({});
		expect(text()).toContain('Billing Price not recorded');
		expect(text()).not.toContain('$0');
		expect(button('Set price')).toBeTruthy();
		button('Set price')!.click(); flushSync();
		expect(button('Clear')).toBeUndefined();
		expect(button('Save')).toBeTruthy();
	});

	it('is read-only for a removed account', () => {
		show({ billing: set, readonly: true });
		expect(text()).toContain('$20.00 a month');
		expect(document.querySelectorAll('button')).toHaveLength(0);
	});

	it('saves cents and day together', async () => {
		const onsave = show({});
		button('Set price')!.click(); flushSync();
		type(price(), '19.99'); type(day(), '31');
		button('Save')!.click(); await settle();
		expect(onsave).toHaveBeenCalledWith({ monthly_price_cents: 1999, charge_day: 31 });
		expect(button('Save')).toBeUndefined();
	});

	it('names both validation problems and does not save', async () => {
		const onsave = show({});
		button('Set price')!.click(); flushSync();
		type(price(), '1.999');
		button('Save')!.click(); await settle();
		expect(text()).toContain('Enter a price of $0 or more with up to two decimals.');
		expect(text()).toContain('Choose a day from 1 to 31.');
		expect(onsave).not.toHaveBeenCalled();
	});

	it('restores the stored value after Cancel', () => {
		show({ billing: set });
		button('Edit')!.click(); flushSync();
		expect((price() as HTMLInputElement).value).toBe('20.00');
		expect((day() as HTMLSelectElement).value).toBe('14');
		type(price(), '99'); type(day(), '2');
		button('Cancel')!.click(); flushSync();
		expect(text()).toContain('$20.00 a month · charged on the 14th');
		button('Edit')!.click(); flushSync();
		expect((price() as HTMLInputElement).value).toBe('20.00');
		expect((day() as HTMLSelectElement).value).toBe('14');
	});

	it('clears with a null pair, offered only when a price is set', async () => {
		const onsave = show({ billing: set });
		button('Edit')!.click(); flushSync();
		button('Clear')!.click(); await settle();
		expect(onsave).toHaveBeenCalledWith({ monthly_price_cents: null, charge_day: null });
	});

	it('shows the server text on a stale save, keeps the draft and reloads on request', async () => {
		const onreload = vi.fn(async () => {});
		const onsave = show({ billing: set, onreload });
		onsave.mockResolvedValueOnce({ ok: false, error: 'This account changed. Reload and try again.', conflict: true });
		button('Edit')!.click(); flushSync();
		type(price(), '25');
		button('Save')!.click(); await settle();
		expect(text()).toContain('This account changed. Reload and try again.');
		expect((price() as HTMLInputElement).value).toBe('25');
		button('Reload')!.click(); await settle();
		expect(onreload).toHaveBeenCalledOnce();
		expect(text()).not.toContain('This account changed.');
	});

	it('shows a server validation error without offering Reload', async () => {
		const onsave = show({});
		onsave.mockResolvedValueOnce({ ok: false, error: 'Enter a price of $0 or more with up to two decimals.' });
		button('Set price')!.click(); flushSync();
		type(price(), '5'); type(day(), '5');
		button('Save')!.click(); await settle();
		expect(text()).toContain('Enter a price of $0 or more');
		expect(button('Reload')).toBeUndefined();
	});
});

describe('BillingEditor on an External endpoint', () => {
	const endpoint = (mode: 'subscription' | 'pay_per_use' | null, listPriced = true) => ({ mode, listPriced });
	const mode = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('[role="radio"]')].find((item) => item.textContent?.trim() === name)!;

	it('reads unset as not recorded, with neither mode selected', () => {
		show({ endpoint: endpoint(null) });
		expect(text()).toContain('Billing Price not recorded');
		button('Set price')!.click(); flushSync();
		expect(mode('Subscription').getAttribute('aria-checked')).toBe('false');
		expect(mode('Pay per use').getAttribute('aria-checked')).toBe('false');
		expect(document.querySelector('select')).toBeNull();
	});

	it('keeps the Billing label while editing an endpoint', () => {
		show({ endpoint: endpoint(null) });
		button('Set price')!.click(); flushSync();
		expect(document.querySelector('.editor .name')?.textContent).toBe('Billing');
	});

	it('requires a mode before saving', async () => {
		const onsave = show({ endpoint: endpoint(null) });
		button('Set price')!.click(); flushSync();
		button('Save')!.click(); await settle();
		expect(text()).toContain('Choose how this endpoint is billed.');
		expect(onsave).not.toHaveBeenCalled();
	});

	it('uses the same price and day fields and copy as an account for Subscription', async () => {
		const onsave = show({ endpoint: endpoint(null) });
		button('Set price')!.click(); flushSync();
		mode('Subscription').click(); flushSync();
		type(price(), '30'); type(day(), '1');
		button('Save')!.click(); await settle();
		expect(onsave).toHaveBeenCalledWith({ mode: 'subscription', monthly_price_cents: 3000, charge_day: 1 });
	});

	it('reads a recorded subscription exactly like an account row', () => {
		show({ billing: set, endpoint: endpoint('subscription') });
		expect(text()).toContain('Billing $20.00 a month · charged on the 14th');
	});

	it('Pay per use hides the fields, says list price when priced, and saves null price and day', async () => {
		const onsave = show({ billing: set, endpoint: endpoint('subscription') });
		button('Edit')!.click(); flushSync();
		mode('Pay per use').click(); flushSync();
		expect(document.querySelector('select')).toBeNull();
		expect(text()).toContain('Counted at list price');
		button('Save')!.click(); await settle();
		expect(onsave).toHaveBeenCalledWith({ mode: 'pay_per_use', monthly_price_cents: null, charge_day: null });
	});

	it('a recorded Pay per use row says whether the host has a list price, never Price not recorded', () => {
		show({ endpoint: endpoint('pay_per_use', true) });
		expect(text()).toContain('Billing Pay per use · Counted at list price');
		expect(text()).not.toContain('Price not recorded');
		unmount(component!); document.body.innerHTML = '';
		show({ endpoint: endpoint('pay_per_use', false) });
		expect(text()).toContain('Billing Pay per use · No list price for this host');
		expect(text()).not.toContain('Price not recorded');
	});

	it('choosing Pay per use keeps the stored price until Save, and Cancel restores it', () => {
		const onsave = show({ billing: set, endpoint: endpoint('subscription') });
		button('Edit')!.click(); flushSync();
		mode('Pay per use').click(); flushSync();
		button('Cancel')!.click(); flushSync();
		expect(onsave).not.toHaveBeenCalled();
		expect(text()).toContain('$20.00 a month · charged on the 14th');
		button('Edit')!.click(); flushSync();
		expect(mode('Subscription').getAttribute('aria-checked')).toBe('true');
		expect((price() as HTMLInputElement).value).toBe('20.00');
	});

	it('Clear sends a null mode with a null pair', async () => {
		const onsave = show({ billing: set, endpoint: endpoint('subscription') });
		button('Edit')!.click(); flushSync();
		button('Clear')!.click(); await settle();
		expect(onsave).toHaveBeenCalledWith({ mode: null, monthly_price_cents: null, charge_day: null });
	});
});
