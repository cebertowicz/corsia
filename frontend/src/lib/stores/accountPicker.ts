import { derived, writable } from 'svelte/store';
import { loadProviderPolicy } from '$lib/api/providerPolicy';
import { modelMatchesId, type Model, type RunsOn } from './models';
import type { VendorAccount } from './vendorAccounts';

/** One account under its vendor's group header in the model picker. */
export interface PickerAccount {
	account: VendorAccount;
	/** The account the next turn runs on: the one `runs_on` entry the catalog marks selected. */
	marked: boolean;
	/** Marked, and chosen during a running turn: the next turn moves to it. */
	pending: boolean;
	/** Rollover position, only while the scope's policy has rollover on. */
	order: number | null;
	/** A person can choose it: signed in and not at its limit. */
	choosable: boolean;
}

/** The effective rollover policy of the open thread's scope, per vendor. */
export interface PickerPolicy {
	rollover: boolean;
	/** Account ids, first is used first. */
	order: string[];
}

/** Keyed by vendor (`provider_key`); a vendor the policy gives no rollover to is absent. */
export const pickerPolicy = writable<Record<string, PickerPolicy>>({});

/**
 * The project `pickerPolicy` was read for: its id, null for the workspace's own, undefined before any read.
 * The map above is one policy, so a reader checks this before trusting it for another project's thread.
 */
export const pickerPolicyProject = writable<string | null | undefined>(undefined);

/**
 * The one way to read the policy for a project and vendor: a reader never compares projects itself.
 * Undefined when the store holds another project's policy (or none), so a caller shows no order and
 * asks `loadPickerPolicy` instead of using a stale one.
 */
export const policyFor = derived(
	[pickerPolicy, pickerPolicyProject],
	([policy, project]) => (projectId: string | null | undefined, vendor: string): PickerPolicy | undefined =>
		project === (projectId ?? null) ? policy[vendor] : undefined,
);

let policyReads = 0;

/**
 * Read the effective policy for a thread's project (the workspace's when it has none). Reads can
 * resolve out of order, so only the newest started read may write; an older success is dropped.
 */
export async function loadPickerPolicy(projectId?: string | null): Promise<void> {
	const read = ++policyReads;
	const result = await loadProviderPolicy(projectId ?? undefined);
	if (read !== policyReads) return;
	if (!result.ok || !Array.isArray(result.value?.providers)) return;
	const next: Record<string, PickerPolicy> = {};
	for (const entry of result.value.providers) {
		if (entry.capabilities.units !== 'accounts' || !entry.capabilities.rollover) continue;
		next[entry.provider_key] = { rollover: entry.rollover, order: entry.order ?? entry.units.map((unit) => unit.id) };
	}
	pickerPolicy.set(next);
	pickerPolicyProject.set(projectId ?? null);
}

/** The entry the catalog marks selected for the thread's next turn: the current model's, else any model's. */
function markedEntry(models: Model[], current?: string | null): RunsOn | null {
	const selected = (model: Model) => model.runs_on?.find((entry) => entry.selected) ?? null;
	const own = models.find((model) => modelMatchesId(model, current) && selected(model));
	const chosen = own ?? models.find(selected);
	return chosen ? selected(chosen) : null;
}

/**
 * The signed-in accounts of one vendor the scope permits, as the catalog says: an account that
 * appears in some `runs_on` entry of the vendor's rows. A removed account never appears.
 */
export function vendorRows(provider: string, all: Model[], accounts: VendorAccount[]): VendorAccount[] {
	const permitted = new Set(all
		.filter((model) => (model.connection ?? model.provider ?? model.backend) === provider)
		.flatMap((model) => (model.runs_on ?? []).map((entry) => entry.account_id)));
	return accounts.filter((account) => account.provider === provider && account.state !== 'removed' && permitted.has(account.id));
}

/**
 * The account rows of one vendor's group. Which accounts appear is what the catalog permits;
 * what each shows is the account's own state from `vendorAccounts`; which is marked is the
 * selected `runs_on` entry. Nothing is derived again here. A vendor with fewer than two such
 * accounts has no account rows, except when the catalog selects none of them: the thread's bound
 * account is then not among the rows (removed, or turned off for the project), and the one
 * account left is the way on, unmarked.
 */
export function pickerAccounts(
	provider: string, all: Model[], accounts: VendorAccount[], policy: PickerPolicy | undefined, current?: string | null,
): PickerAccount[] {
	let rows = vendorRows(provider, all, accounts);
	const marked = markedEntry(all.filter((model) => (model.connection ?? model.provider ?? model.backend) === provider), current);
	if (rows.length < 2 && !(rows.length === 1 && marked === null)) return [];
	const ordered = policy?.rollover === true;
	if (ordered) {
		const rank = (account: VendorAccount) => {
			const index = policy.order.indexOf(account.id);
			return index < 0 ? Number.MAX_SAFE_INTEGER : index;
		};
		rows = [...rows].sort((a, b) => rank(a) - rank(b));
	}
	return rows.map((account, index) => ({
		account,
		marked: account.id === marked?.account_id,
		pending: account.id === marked?.account_id && marked.pending === true,
		order: ordered ? index + 1 : null,
		choosable: account.state !== 'signed_out' && account.usage?.state !== 'limited',
	}));
}
