export type BillingMode = 'subscription' | 'pay_per_use';
export interface BillingValue { monthly_price_cents: number; charge_day: number }

export const PRICE_ERROR = 'Enter a price of $0 or more with up to two decimals.';
export const DAY_ERROR = 'Choose a day from 1 to 31.';
export const UNSET_COST = 'Price not recorded';
export const LIST_PRICE = 'Counted at list price';
export const NO_LIST_PRICE = 'No list price for this host';

const MAX_PRICE_CENTS = 10_000_000;
const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Whole cents from a typed dollar amount, or null when it is not $0 or more with up to two decimals. */
export function parsePriceCents(text: string): number | null {
	const match = /^\$?(\d{1,7})(?:\.(\d{1,2}))?$/.exec(text.trim());
	if (!match) return null;
	const cents = Number(match[1]) * 100 + Number((match[2] ?? '').padEnd(2, '0'));
	return cents <= MAX_PRICE_CENTS ? cents : null;
}

export function parseChargeDay(text: string): number | null {
	if (!/^\d{1,2}$/.test(text)) return null;
	const day = Number(text);
	return day >= 1 && day <= 31 ? day : null;
}

export function ordinal(day: number): string {
	const teen = day % 100 >= 11 && day % 100 <= 13;
	const suffix = teen ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[day % 10] ?? 'th';
	return `${day}${suffix}`;
}

export function formatUsd(cents: number): string {
	return usd.format(cents / 100);
}

export function formatMonthly(billing: BillingValue): string {
	return `${usd.format(billing.monthly_price_cents / 100)} a month · charged on the ${ordinal(billing.charge_day)}`;
}

/** The collapsed row's text. */
export function billingSummary(billing: BillingValue | null, endpoint: { mode: BillingMode | null; listPriced: boolean } | null): { text: string } {
	if (!endpoint) return { text: billing ? formatMonthly(billing) : 'Price not recorded' };
	if (endpoint.mode === null) return { text: UNSET_COST };
	if (endpoint.mode === 'pay_per_use') return { text: `Pay per use · ${endpoint.listPriced ? LIST_PRICE : NO_LIST_PRICE}` };
	return { text: billing ? formatMonthly(billing) : 'Price not recorded' };
}
