import { derived } from 'svelte/store';
import { pickerAccounts, type PickerPolicy } from './accountPicker';
import { HARNESSES } from './harnessMembership';
import { resetLabel } from './modelGroups';
import { accountTurn, activeModelId, findModelById, models, modelAvailability, type Model, type RunsOn } from './models';
import { activeThreadId } from './threads';
import type { VendorAccount } from './vendorAccounts';

/** Identical for Claude Code and Codex. */
export const OVERAGE_SWITCH_LABEL = 'Spend past usage limit';
export const PAUSED_AT_USAGE_LIMIT = 'Paused: usage limit reached';
/** The held line's picker action (#2477 amendment 17). */
export const SWITCH_MODEL_LABEL = 'Switch model';
/** The verdict reason and Stop cause the backend writes while the switch is off. */
export const PLAN_LIMIT_REASON = 'plan_limit_overage_off';

/** `action` moves the thread to another account; `allLimited` says there is none, so no way out is offered. */
export type HeldLine = {
	text: string;
	planLimit: boolean;
	action?: { accountId: string; label: string };
	allLimited?: true;
};

/** What the account sentences read: the vendor's accounts (labels) and the scope's rollover order, as the picker has them. */
export interface HeldLineAccounts {
	accounts: VendorAccount[];
	policy?: PickerPolicy;
}

/** One account-owned usage-limit sentence for Settings and held-thread surfaces. */
export function formatUsageLimit(
	usage: { reason?: string | null; resets_at?: string | null },
	now: Date = new Date(),
): string {
	const reset = resetLabel(usage.resets_at, true, now);
	const tail = reset ? `resets ${reset}` : 'reset time unknown';
	return `${usage.reason === PLAN_LIMIT_REASON ? 'Plan' : 'Usage'} limit reached · ${tail}`;
}

const limitedEntry = (entry: RunsOn) => entry.status_reason === 'usage_limited';
const resetOf = (entry: RunsOn) => entry.usage?.resets_at ?? entry.resets_at;

/** The picker's "<prefix> <time>" form, or its tail when no reset is known. */
function backTail(iso: string | null | undefined, prefix: string, now: Date): string {
	const back = resetLabel(iso, true, now);
	return back ? `${prefix} ${back}` : 'reset time unknown';
}

/**
 * The held line when a vendor has several accounts and the bound one is at its limit: either every
 * permitted account is limited (no way out to offer), or another one is ready and the line names
 * both. Which accounts are permitted and ready is the catalog's `runs_on`; which comes first is the
 * picker's order. Anything else (one account, a plan limit, no ready account, labels not loaded) is
 * not decided here and reads as before.
 */
function accountLine(model: Model, now: Date, context: HeldLineAccounts | undefined): HeldLine | null {
	const entries = model.runs_on ?? [];
	const bound = entries.find((entry) => entry.selected);
	if (!bound || entries.length < 2) return null;
	if (entries.every(limitedEntry)) {
		const vendor = HARNESSES.find((entry) => entry.name === model.connection)?.vendor;
		if (!vendor) return null;
		const earliest = entries.map(resetOf).filter((iso): iso is string => !!iso && resetLabel(iso) !== '')
			.sort((a, b) => new Date(a).getTime() - new Date(b).getTime())[0];
		return {
			text: `All ${vendor} accounts are at their limit · ${backTail(earliest, 'first back', now)}`,
			planLimit: false,
			allLimited: true,
		};
	}
	if (!context || !model.connection) return null;
	const rows = pickerAccounts(model.connection, [model], context.accounts, context.policy, model.id);
	const limited = rows.find((row) => row.account.id === bound.account_id);
	const ready = new Set(entries.filter((entry) => !entry.selected && entry.status === 'ready').map((entry) => entry.account_id));
	const next = rows.find((row) => ready.has(row.account.id));
	if (!limited || !next) return null;
	return {
		text: `${limited.account.label.text} is at its limit · ${backTail(resetOf(bound), 'back', now)}`,
		planLimit: false,
		action: { accountId: next.account.id, label: `Continue on ${next.account.label.text}` },
	};
}

/**
 * The held-thread line for any limited bound model, or null while it can run; `planLimit` gates the
 * switch link. `context` carries the account labels: without it (no thread to move, or accounts not
 * loaded) the account sentence with an action is not offered and the line reads as it always has.
 */
export function limitHeldLine(model: Model | undefined, now: Date = new Date(), context?: HeldLineAccounts): HeldLine | null {
	const usage = model && modelAvailability(model).usage;
	if (!model || usage?.state !== 'limited') return null;
	const planLimit = usage.reason === PLAN_LIMIT_REASON;
	return (!planLimit && accountLine(model, now, context)) || { text: formatUsageLimit(usage, now), planLimit };
}

/** The active thread while its bound model row is usage-limited, else null: the read-time hold queued labels show. */
export const limitHeldThreadId = derived(
	[models, activeModelId, activeThreadId],
	([$models, $activeModelId, $activeThreadId]) => {
		const model = findModelById($models, $activeModelId);
		// The bound account's own limit holds the input, whether or not another account could run the model.
		return model && accountTurn(model).status_reason === 'usage_limited' ? $activeThreadId : null;
	},
);
