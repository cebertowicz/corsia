import { get, writable } from 'svelte/store';
import { token } from './auth';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';
import type { ModelUsage } from './modelUsage';

const BASE = '';
const auth = () => get(token) || '';

export type VendorAccountState = 'legacy' | 'signed_in' | 'signed_out' | 'removed';
export type SigninStatus = 'waiting' | 'completed' | 'failed' | 'refused' | 'cancelled' | 'superseded' | 'unavailable';

export interface VendorAccountLabel {
	name: string;
	email: string | null;
	text: string;
	custom: string | null;
}

/** An account's recorded subscription price; the account's billing is null when none is recorded. */
export interface AccountBilling {
	monthly_price_cents: number;
	charge_day: number;
}

export interface VendorAccountUsage extends ModelUsage {
	/** A background account-usage recheck is still running. */
	checking: boolean;
	/** The most recent usage check, including any failure the account row should show. */
	check: { outcome?: string; at?: string; error?: string | null } | null;
}

export interface VendorAccount {
	id: string;
	provider: string;
	name: string;
	is_default: boolean;
	state: VendorAccountState;
	revision: number;
	identity: Record<string, string>;
	label: VendorAccountLabel;
	duplicate_of?: string | null;
	/** This account's 'Spend past plan limit' switch; null where no plan limit applies. */
	allow_overage?: boolean | null;
	/** Signed in, but none of the vendor's models can run on this account. */
	no_usable_model?: boolean;
	/** Account-owned plan usage, rendered once in its Settings account row. */
	usage?: VendorAccountUsage;
	/** The recorded monthly price and charge day; null when no price is recorded. */
	billing?: AccountBilling | null;
	/** Fences billing writes; sent back unchanged. */
	billing_revision?: number;
	/** A receipt confirmed state, but account details/revision require an explicit reload. */
	details_stale?: boolean;
}

export interface SigninResult {
	account_id: string;
	route: 'browser' | null;
	status: SigninStatus;
	cause: string | null;
	duplicate_of?: string | null;
	held_by?: string | null;
}

export const vendorAccounts = writable<VendorAccount[]>([]);
// Confirmations invalidate in-flight inventories, including equal-revision receipts.
// Among unchanged confirmations, the latest successful request wins; failures do not.
let confirmationSequence = 0;
let readSequence = 0;
let acceptedRead = 0;

function confirmAccounts(update: (accounts: VendorAccount[]) => VendorAccount[]) {
	confirmationSequence += 1;
	vendorAccounts.update(update);
}

function url(path: string, query: Record<string, string> = {}): string {
	const params = new URLSearchParams({ token: auth(), ...query });
	return `${BASE}${path}?${params.toString()}`;
}

async function detail(response: Response, fallback: string): Promise<{ error: string; code?: string }> {
	const body = await response.json().catch(() => null);
	return { error: plainErrorText(body?.detail, fallback), ...(typeof body?.code === 'string' ? { code: body.code } : {}) };
}

function replace(account: VendorAccount): VendorAccount {
	confirmAccounts((accounts) => accounts.map((row) => row.id === account.id ? account : row));
	return account;
}

async function request<T>(path: string, init: RequestInit, fallback: string, query?: Record<string, string>): Promise<{ ok: true; data: T } | { ok: false; error: string; status?: number; code?: string }> {
	try {
		const response = await fetch(url(path, query), init);
		if (!response.ok) return { ok: false, ...await detail(response, fallback), status: response.status };
		return { ok: true, data: await response.json() as T };
	} catch {
		return { ok: false, error: SERVER_UNREACHABLE };
	}
}

function json(method: string, body?: object): RequestInit {
	return body === undefined
		? { method }
		: { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

export async function loadVendorAccounts(includeRemoved = false): Promise<{ ok: boolean; error?: string; superseded?: boolean }> {
	const confirmationAtStart = confirmationSequence;
	const read = ++readSequence;
	const result = await request<{ accounts: VendorAccount[] }>('/api/vendor-accounts', { method: 'GET' }, 'Could not load accounts.', includeRemoved ? { include_removed: 'true' } : {});
	if (!result.ok) return result;
	if (!Array.isArray(result.data?.accounts)) return { ok: false, error: 'Could not read accounts.' };
	if (confirmationAtStart !== confirmationSequence || read < acceptedRead) {
		return { ok: false, superseded: true, error: 'Account details changed while loading. Reload again.' };
	}
	acceptedRead = read;
	vendorAccounts.set(result.data.accounts);
	return { ok: true };
}

/** The removed accounts, read without touching the shared list that every other surface renders. */
export async function fetchRemovedVendorAccounts(): Promise<{ ok: true; accounts: VendorAccount[] } | { ok: false; error: string }> {
	const result = await request<{ accounts: VendorAccount[] }>('/api/vendor-accounts', { method: 'GET' }, 'Could not load accounts.', { include_removed: 'true' });
	if (!result.ok) return result;
	if (!Array.isArray(result.data?.accounts)) return { ok: false, error: 'Could not read accounts.' };
	return { ok: true, accounts: result.data.accounts.filter((account) => account.state === 'removed') };
}

export async function addVendorAccount(provider: string, name: string): Promise<{ ok: boolean; account?: VendorAccount; error?: string }> {
	const result = await request<VendorAccount>('/api/vendor-accounts', json('POST', { provider, name }), 'Could not add account.');
	if (!result.ok) return result;
	confirmAccounts((accounts) => [...accounts, result.data]);
	return { ok: true, account: result.data };
}

function staleAccount(account: VendorAccount): boolean {
	return !!account.details_stale || !!get(vendorAccounts).find((row) => row.id === account.id)?.details_stale;
}

const RELOAD_ACCOUNT = 'Reload account details before acting.';

export async function renameVendorAccount(account: VendorAccount, name: string): Promise<{ ok: boolean; error?: string }> {
	if (staleAccount(account)) return { ok: false, error: RELOAD_ACCOUNT };
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}`, json('PATCH', {
		name, expected_revision: account.revision,
	}), 'Could not rename account.');
	if (!result.ok) {
		if (result.status === 409) replace({ ...account, details_stale: true });
		return result;
	}
	replace(result.data);
	return { ok: true };
}

async function refreshAfterMutation(success: string): Promise<{ ok: boolean; error?: string }> {
	const refreshed = await loadVendorAccounts();
	return refreshed.ok ? { ok: true } : { ok: false, error: `${success} The account list could not be reloaded.` };
}

/** Record the account's monthly price and charge day; a null pair clears them. */
export async function setVendorAccountBilling(account: VendorAccount, input: {
	monthly_price_cents: number | null; charge_day: number | null;
}): Promise<{ ok: boolean; error?: string; status?: number; code?: string }> {
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}/billing`, json('PUT', {
		...input,
		expected_billing_revision: account.billing_revision ?? 0,
	}), 'Could not save billing.');
	if (!result.ok) return result;
	replace(result.data);
	return { ok: true };
}

/** Record overage charged for one month, "YYYY-MM". */
export async function recordVendorAccountOverage(account: VendorAccount, input: {
	amount_cents: number; month: string;
}): Promise<{ ok: boolean; error?: string; status?: number; code?: string }> {
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}/overage`, json('POST', {
		...input,
		expected_billing_revision: account.billing_revision ?? 0,
	}), 'Could not save overage.');
	if (!result.ok) return result;
	replace(result.data);
	return { ok: true };
}

/** Store one account's 'Spend past plan limit' switch; `error` is user-facing text. */
export async function setVendorAccountOverage(account: VendorAccount, allow: boolean): Promise<{ ok: boolean; error?: string }> {
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}/usage`, json('PUT', {
		allow_overage: allow,
	}), 'try again.');
	if (!result.ok) return result;
	replace(result.data);
	return { ok: true };
}

export async function startVendorSignin(accountId: string): Promise<{ ok: boolean; signin?: SigninResult; error?: string }> {
	const result = await request<SigninResult>(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin`, json('POST'), 'Could not start sign-in.');
	if (!result.ok) return result;
	return reconcileSignin(result.data);
}

async function reconcileSignin(signin: SigninResult): Promise<{ ok: boolean; signin: SigninResult; error?: string }> {
	if (signin.status !== 'completed') return { ok: true, signin };
	confirmAccounts((accounts) => accounts.map((account) => account.id === signin.account_id
		? { ...account, state: 'signed_in', details_stale: true } : account));
	const refreshed = await loadVendorAccounts();
	return refreshed.ok
		? { ok: true, signin }
		: { ok: true, signin, error: 'Signed in, but the account list could not be reloaded.' };
}

export async function checkVendorSignin(accountId: string): Promise<{ ok: boolean; signin?: SigninResult; absent?: boolean; error?: string }> {
	const result = await request<SigninResult>(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin/check`, json('POST'), 'Could not check sign-in.');
	if (!result.ok) return result.error === 'no sign-in is in progress for this account' ? { ok: false, absent: true } : result;
	return reconcileSignin(result.data);
}

export async function cancelVendorSignin(accountId: string): Promise<{ ok: boolean; signin?: SigninResult; error?: string }> {
	const result = await request<SigninResult>(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin`, json('DELETE'), 'Could not cancel sign-in.');
	if (!result.ok) return result;
	return reconcileSignin(result.data);
}

async function applyThenRefresh(account: VendorAccount, success: string): Promise<{ ok: boolean; account: VendorAccount; error?: string }> {
	replace(account);
	const refreshed = await refreshAfterMutation(success);
	return refreshed.ok ? { ok: true, account } : { ok: true, account, error: refreshed.error };
}

export async function signOutVendorAccount(account: VendorAccount): Promise<{ ok: boolean; account?: VendorAccount; error?: string }> {
	if (staleAccount(account)) return { ok: false, error: RELOAD_ACCOUNT };
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}/signout`, json('POST', {
		expected_revision: account.revision,
	}), 'Could not sign out.');
	if (!result.ok) return result;
	return applyThenRefresh(result.data, 'The account was signed out.');
}

export async function removeVendorAccount(account: VendorAccount): Promise<{ ok: boolean; account?: VendorAccount; error?: string }> {
	if (staleAccount(account)) return { ok: false, error: RELOAD_ACCOUNT };
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}`, json('DELETE', {
		expected_revision: account.revision,
	}), 'Could not remove account.');
	if (!result.ok) return result;
	return applyThenRefresh(result.data, 'The account was removed.');
}
