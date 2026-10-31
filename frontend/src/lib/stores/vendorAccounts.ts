import { get, writable } from 'svelte/store';
import { token } from './auth';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';
import type { VendorAccountState, SigninStatus, VendorAccountLabel, AccountBilling, VendorAccountUsage, VendorAccount, SigninResult } from './vendorAccountTypes';
export type { VendorAccountState, SigninStatus, VendorAccountLabel, AccountBilling, VendorAccountUsage, VendorAccount, SigninResult } from './vendorAccountTypes';

const BASE = '';
const auth = () => get(token) || '';

export function visibleVendorAccounts(accounts: VendorAccount[], provider: string, removed: VendorAccount[]): VendorAccount[] {
	return [...accounts.filter((account) => account.provider === provider && account.state !== 'removed' && !removed.some((row) => row.id === account.id)), ...removed];
}

export function rememberVisibleSignin(signins: Record<string, SigninResult>, result: SigninResult, accounts: VendorAccount[]): Record<string, SigninResult> {
	// A same-account sign-in removes the added row, so its message moves to the account it reached.
	const listed = (id: string | null | undefined) => accounts.some((account) => account.id === id);
	const key = listed(result.account_id) ? result.account_id : result.cause === 'same_account' && listed(result.duplicate_of) ? result.duplicate_of : null;
	const { [result.account_id]: _, ...rest } = signins;
	return key ? { ...rest, [key]: result } : rest;
}

/** A list-reload failure line, with its support reference when the server gave one. */
export function reloadErrorText(error: string, ref?: string): string {
	return ref ? `${error} Reference: ${ref}` : error;
}

export const vendorAccounts = writable<VendorAccount[]>([]);
/** One live sign-in state per account, shared by Settings and every held chat. */
export const signin = writable<Record<string, SigninResult>>({});
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

async function detail(response: Response, fallback: string): Promise<{ error: string; code?: string; ref?: string }> {
	const body = await response.json().catch(() => null);
	const ref = typeof body?.ref === 'string' ? body.ref : typeof body?.detail?.ref === 'string' ? body.detail.ref : '';
	return { error: plainErrorText(body?.detail, fallback), ...(typeof body?.code === 'string' ? { code: body.code } : {}), ...(ref ? { ref } : {}) };
}

function replace(account: VendorAccount): VendorAccount {
	confirmAccounts((accounts) => accounts.map((row) => row.id === account.id ? account : row));
	return account;
}

async function request<T>(path: string, init: RequestInit, fallback: string, query?: Record<string, string>): Promise<{ ok: true; data: T } | { ok: false; error: string; status?: number; code?: string; ref?: string }> {
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

export async function loadVendorAccounts(includeRemoved = false): Promise<{ ok: boolean; error?: string; ref?: string; superseded?: boolean }> {
	const confirmationAtStart = confirmationSequence;
	const read = ++readSequence;
	const result = await request<{ accounts: VendorAccount[] }>('/api/vendor-accounts', { method: 'GET' }, 'Couldn’t load your accounts.', includeRemoved ? { include_removed: 'true' } : {});
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
	const result = await request<{ accounts: VendorAccount[] }>('/api/vendor-accounts', { method: 'GET' }, 'Couldn’t load your accounts.', { include_removed: 'true' });
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

/** Store one account's 'Spend past usage limit' switch; `error` is user-facing text. */
export async function setVendorAccountOverage(account: VendorAccount, allow: boolean): Promise<{ ok: boolean; error?: string }> {
	const result = await request<VendorAccount>(`/api/vendor-accounts/${encodeURIComponent(account.id)}/usage`, json('PUT', {
		allow_overage: allow,
	}), 'try again.');
	if (!result.ok) return result;
	replace(result.data);
	return { ok: true };
}

export async function startVendorSignin(accountId: string): Promise<{ ok: boolean; signin?: SigninResult; error?: string }> {
	const wasSignedIn = get(vendorAccounts).some((account) => account.id === accountId && account.state === 'signed_in');
	const result = await request<SigninResult>(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin`, json('POST'), 'Could not start sign-in.');
	if (!result.ok) return result;
	if (wasSignedIn && result.data.status === 'waiting') {
		const refreshed = await loadVendorAccounts();
		if (!refreshed.ok && !refreshed.superseded) return { ok: true, signin: result.data, error: reloadErrorText(refreshed.error ?? 'Couldn’t load your accounts.', refreshed.ref) };
	}
	return reconcileSignin(result.data);
}

async function reconcileSignin(signin: SigninResult): Promise<{ ok: boolean; signin: SigninResult; error?: string }> {
	if (signin.status === 'superseded' && signin.cause === 'account_refresh_superseded') {
		await loadVendorAccounts();
		return { ok: true, signin };
	}
	const signedIn = signin.status === 'completed' || signin.status === 'already_signed_in';
	if (!signedIn && signin.status !== 'cancelled' && signin.cause !== 'same_account') return { ok: true, signin };
	if (signedIn) {
		confirmAccounts((accounts) => accounts.map((account) => account.id === signin.account_id
			? { ...account, state: 'signed_in', details_stale: true } : account));
	}
	const refreshed = await loadVendorAccounts();
	if (refreshed.ok || refreshed.superseded) return { ok: true, signin };
	return { ok: true, signin, error: reloadErrorText(signedIn ? 'Signed in, but the account list could not be reloaded.' : 'Couldn’t load your accounts.', refreshed.ref) };
}

export async function checkVendorSignin(accountId: string): Promise<{ ok: boolean; signin?: SigninResult; absent?: boolean; error?: string }> {
	const result = await request<SigninResult>(`/api/vendor-accounts/${encodeURIComponent(accountId)}/signin/check`, json('POST'), 'Could not check sign-in.');
	if (!result.ok) return result.error === 'no sign-in is in progress for this account' ? { ok: false, absent: true } : result;
	return reconcileSignin(result.data);
}

export async function refreshVendorModels(accountId: string): Promise<{ ok: boolean; signedOut?: boolean; superseded?: boolean; error?: string; ref?: string }> {
	const result = await request<{ state: VendorAccountState; vendor_signed_in: boolean | null; model_refresh_ok: boolean | null; model_refresh_cause: string | null; model_refresh_ref: string | null }>(`/api/vendor-accounts/${encodeURIComponent(accountId)}/verify`, json('POST'), 'Couldn’t refresh models. Try again.');
	if (!result.ok) return result;
	if (result.data.state === 'signed_out') {
		confirmAccounts((accounts) => accounts.map((account) => account.id === accountId ? { ...account, state: 'signed_out', details_stale: true } : account));
		const refreshed = await loadVendorAccounts();
		return refreshed.ok || refreshed.superseded ? { ok: true, signedOut: true } : { ok: false, signedOut: true, error: reloadErrorText(refreshed.error ?? 'Couldn’t load your accounts. Try again.', refreshed.ref) };
	}
	if (result.data.vendor_signed_in === true && result.data.model_refresh_ok === true) return { ok: true };
	// A newer login replaced the one this refresh read: nothing failed, and the reload shows the account as it now is.
	if (result.data.model_refresh_cause === 'account_refresh_superseded') {
		await loadVendorAccounts();
		return { ok: true, superseded: true };
	}
	return { ok: false, error: 'The model list couldn’t be refreshed.', ref: result.data.model_refresh_ref ?? undefined };
}

/** Reload an account's model list and record the outcome as its one sign-in line, for Settings and chat alike. */
export async function refreshAndRecordModels(accountId: string): ReturnType<typeof refreshVendorModels> {
	const result = await refreshVendorModels(accountId);
	if (result.signedOut || result.superseded) signin.update(({ [accountId]: _, ...rest }) => rest);
	else signin.update((current) => ({ ...current, [accountId]: { account_id: accountId, route: null, status: result.ok ? 'already_signed_in' : 'model_refresh_failed', cause: null, ref: result.ref } }));
	return result;
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
