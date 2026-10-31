import { HARNESSES, pickerCatalogModels, withoutStaleSiblings } from './harnessMembership';
import { modelAvailability, type Model } from './models';
import type { UsageWindow } from './modelUsage';
import type { VendorAccount } from './vendorAccounts';
import { earliestReset, resetLabel } from './resetTime';

export { resetLabel };

export type GroupState = 'ready' | 'limited' | 'signed_out' | 'unavailable';

/** One account in the picker: the rows of a connection and the state its rows report. */
export interface ModelGroup {
	key: string;
	name: string;
	state: GroupState;
	/** Rows a person can pick, in catalog order. */
	ready: Model[];
	/** Configured rows that cannot be picked, in catalog order. */
	blocked: Model[];
	/** Every row the group offers, picked or not, in catalog order. */
	rows: Model[];
	/** Reset of a limited group, from the rows' availability. */
	resetsAt: string | null;
}

export function canonicalName(model: Model): string {
	return model.display_name ?? model.name;
}

function groupKey(model: Model): string {
	const connection = model.connection ?? model.provider ?? model.backend;
	return model.backend === 'network' && model.server_name ? `${connection}:${model.server_name}` : connection;
}

function groupName(model: Model): string {
	const connection = model.connection ?? model.provider ?? model.backend;
	const harness = HARNESSES.find((entry) => entry.name === connection);
	return harness?.label ?? (model.server_name || connection);
}

/**
 * Group catalog rows by connection. Every state is read from each row's availability
 * reading (`modelAvailability`: status, reason, reset); nothing is derived from anywhere
 * else. What an account has left is not read here: it belongs to the account
 * (`vendorAccounts`), and the picker takes it from there.
 * A row that is not ready is offered only when it is unavailable with a reason
 * (one reason is all a person is told), so a local model not loaded, or one
 * with no reason, is not offered and a group of only such rows is omitted.
 * Membership is `isPickerMember`, the one alias rule every picker uses.
 */
export function groupModels(all: Model[], current?: string | null): ModelGroup[] {
	const groups = new Map<string, ModelGroup>();
	for (const model of pickerCatalogModels(all, current)) {
		const turn = modelAvailability(model);
		const isReady = turn.status === 'ready';
		const key = groupKey(model);
		let group = groups.get(key);
		if (!group) {
			group = {
				key, name: groupName(model), state: 'ready', ready: [], blocked: [], rows: [], resetsAt: null,
			};
			groups.set(key, group);
		}
		(isReady ? group.ready : group.blocked).push(model);
	}
	for (const group of groups.values()) {
		const offered = new Set(withoutStaleSiblings([...group.ready, ...group.blocked], current));
		group.blocked = group.blocked.filter((model) => offered.has(model));
		group.rows = all.filter((model) => offered.has(model));
		if (group.ready.length > 0) continue;
		const reasons = new Set(group.blocked.map((model) => modelAvailability(model).status_reason));
		if (reasons.size === 1 && reasons.has('usage_limited')) group.state = 'limited';
		else if (reasons.has('connection_unavailable')) group.state = 'signed_out';
		else group.state = 'unavailable';
		group.resetsAt = group.blocked.map((model) => modelAvailability(model).resets_at).find(Boolean) ?? null;
	}
	return [...groups.values()];
}

/** Short window name from its length, else its kind. */
export function windowLabel(window: UsageWindow): string {
	const minutes = window.window_minutes;
	if (typeof minutes === 'number' && minutes > 0) {
		if (minutes % 10080 === 0) return minutes === 10080 ? 'wk' : `${minutes / 10080}w`;
		if (minutes % 1440 === 0) return `${minutes / 1440}d`;
		if (minutes % 60 === 0) return `${minutes / 60}h`;
		return `${minutes}m`;
	}
	if (window.kind.startsWith('five_hour')) return '5h';
	if (window.kind.startsWith('seven_day')) return 'wk';
	return window.kind.replace(/^(claude|codex)[:_]/, '').replace(/_/g, ' ');
}

export type UsageLevel = 'normal' | 'amber' | 'red';

/** One window's usage read both ways, with its warning level. */
export interface WindowReading {
	/** Whole percent left, rounded down so it never overstates room; 0 with `used` 99 reads `<1%`. */
	left: number;
	/** Whole percent used, rounded up and held at 99 until the window is actually spent. */
	used: number;
	level: UsageLevel;
}

/** Percent used counts up for every vendor; left and used both derive from it. The level reads the raw value. */
export function windowReading(rawUsed: number): WindowReading {
	const raw = Math.min(100, Math.max(0, rawUsed));
	const spent = raw >= 100;
	const used = spent ? 100 : Math.min(99, Math.ceil(raw));
	const left = spent ? 0 : Math.floor(100 - raw);
	const level: UsageLevel = raw >= 90 ? 'red' : raw >= 75 ? 'amber' : 'normal';
	return { left, used, level };
}

function leftText(reading: WindowReading): string {
	return reading.left === 0 && reading.used < 100 ? '<1%' : `${reading.left}%`;
}

/** A piece of picker text and how loudly to show it. */
export interface UsageSegment {
	text: string;
	level: UsageLevel;
	/** Spelled-out equivalent for assistive technology; absent when the text already reads in full. */
	label?: string;
}

function checkInProgress(usage: VendorAccount['usage']): boolean {
	// A completed check wins over a stale in-flight marker from the catalog snapshot.
	return usage?.checking === true && !usage.check?.outcome && !usage.check?.error;
}

/** A limited status keeps a known reset visible when a recheck cannot verify it. */
function limitedStatus(prefix: string, reset: string, resetWord: string, failed: boolean): string {
	if (!reset) return failed ? "limited · couldn't verify" : prefix;
	const status = `${prefix} · ${resetWord} ${reset}`;
	return failed ? `${status} · couldn't verify` : status;
}

export function accountSpendingSegment(account: VendorAccount): UsageSegment | null {
	const usage = account.usage;
	const planSpent = usage?.windows.some((window) => !window.scope_model && reported(window)
		&& (window.used_percent as number) >= 100);
	if (usage?.state !== 'allowed' || usage.allow_overage !== true || !planSpent) return null;
	const facts = usage.facts;
	const credits = facts?.credits;
	if (account.provider === 'codex' && typeof credits === 'object' && credits !== null
		&& ((credits as Record<string, unknown>).hasCredits === true || (credits as Record<string, unknown>).unlimited === true)) {
		return { text: 'plan used · spending credits', level: 'amber' };
	}
	const extra = facts?.extra_usage;
	if (account.provider === 'claude-code' && typeof extra === 'object' && extra !== null
		&& (extra as Record<string, unknown>).is_enabled === true) {
		return { text: 'plan used · spending extra usage', level: 'amber' };
	}
	return null;
}

/** Window length spelled out for an accessible label. */
function windowName(window: UsageWindow): string {
	const minutes = window.window_minutes;
	if (typeof minutes === 'number' && minutes > 0) {
		if (minutes === 10080) return 'weekly';
		if (minutes % 10080 === 0) return `${minutes / 10080}-week`;
		if (minutes % 1440 === 0) return `${minutes / 1440}-day`;
		if (minutes % 60 === 0) return `${minutes / 60}-hour`;
		return `${minutes}-minute`;
	}
	if (window.kind.startsWith('five_hour')) return '5-hour';
	if (window.kind.startsWith('seven_day')) return 'weekly';
	return windowLabel(window);
}

/** Header or row-note text for a reported window: what is left, at its own warning level. */
function windowSegment(window: UsageWindow): UsageSegment {
	const reading = windowReading(window.used_percent as number);
	const left = leftText(reading);
	return {
		text: `${windowLabel(window)} ${left} left`,
		level: reading.level,
		label: `${windowName(window)}: ${left === '<1%' ? 'less than 1%' : left} left`,
	};
}

/** An account row's text for one window: what is left at its level, or that the window has no reading. */
export function accountWindowSegment(window: UsageWindow): UsageSegment {
	if (reported(window)) return windowSegment(window);
	return { text: `${windowLabel(window)} usage unknown`, level: 'normal', label: `${windowName(window)}: usage unknown` };
}

/** Breakdown line: both numbers and the reset; a window with no reading says so. */
function breakdownLine(window: UsageWindow): string {
	const label = windowLabel(window);
	if (!reported(window)) return `${label} · usage unknown`;
	const reading = windowReading(window.used_percent as number);
	const reset = resetLabel(window.used_percent_resets_at ?? window.resets_at, true);
	return `${label} · ${leftText(reading)} left · ${reading.used}% used${reset ? ` · resets ${reset}` : ''}`;
}

function reported(window: UsageWindow): boolean {
	return window.state === 'reported' && typeof window.used_percent === 'number';
}

/** Window length in minutes, from the reported length else the kind; unknown lengths sort last. */
function windowMinutes(window: UsageWindow): number {
	if (typeof window.window_minutes === 'number' && window.window_minutes > 0) return window.window_minutes;
	if (window.kind.startsWith('five_hour')) return 300;
	if (window.kind.startsWith('seven_day')) return 10080;
	return Number.POSITIVE_INFINITY;
}

/** Shortest window first: the order the header and its breakdown both read. */
function byLength(windows: UsageWindow[]): UsageWindow[] {
	return [...windows].sort((a, b) => windowMinutes(a) - windowMinutes(b));
}

/** Reported windows for the header, shortest first. A label appears once: of two windows that read the same, the one closer to its limit. */
function headerWindows(windows: UsageWindow[]): UsageWindow[] {
	const byLabel = new Map<string, UsageWindow>();
	for (const window of byLength(windows.filter(reported))) {
		const label = windowLabel(window);
		const shown = byLabel.get(label);
		if (!shown || (window.used_percent as number) > (shown.used_percent as number)) byLabel.set(label, window);
	}
	return [...byLabel.values()];
}

/** What an account shows in the picker, at each window's own level: its figures, or the one state that replaces them. */
export function accountSegments(account: VendorAccount): UsageSegment[] {
	if (account.state === 'signed_out') return [{ text: 'signed out', level: 'normal' }];
	const usage = account.usage;
	if (usage?.state === 'limited') {
		if (checkInProgress(usage)) return [{ text: 'limited · checking', level: 'red' }];
		const back = resetLabel(usage.resets_at, true);
		return [{ text: limitedStatus('limited', back, 'back', Boolean(usage.check?.error)), level: 'red' }];
	}
	const spending = accountSpendingSegment(account);
	if (spending) return [spending];
	const shown = headerWindows((usage?.windows ?? []).filter((window) => !window.scope_model));
	return shown.length > 0 ? shown.map(windowSegment) : [{ text: 'usage unknown', level: 'normal' }];
}

/**
 * Header pieces for a group. A vendor with one account shows that account's figures here and
 * nothing else; with several, each account's row carries its own, so a ready group shows none.
 * A group that cannot be picked from says so in words.
 */
export function groupStatusSegments(group: ModelGroup, accounts: VendorAccount[] = []): UsageSegment[] {
	if (group.state === 'ready') return accounts.length === 1 ? accountSegments(accounts[0]) : [];
	if (group.state === 'limited') {
		const usages = accounts.map((account) => account.usage);
		if (usages.some(checkInProgress)) return [{ text: 'limited · checking', level: 'red' }];
		const severalAccounts = accounts.length > 1;
		// With several accounts the group's own reset is one account's; first back is the soonest of them all.
		const back = resetLabel((severalAccounts && earliestReset(usages.map((usage) => usage?.resets_at))) || group.resetsAt, true);
		const prefix = severalAccounts ? `All ${accounts.length} accounts limited` : 'limited';
		return [{ text: limitedStatus(prefix, back, severalAccounts ? 'first back' : 'back', usages.some((usage) => usage?.check?.error)), level: 'red' }];
	}
	return [{ text: group.state === 'signed_out' ? 'signed out' : 'unavailable', level: 'normal' }];
}

/** Every window with both numbers and its reset, one per line, shortest first; a stale window reads unknown. */
export function usageBreakdown(account: VendorAccount): string[] {
	return byLength((account.usage?.windows ?? []).filter((window) => !window.scope_model)).map(breakdownLine);
}

/** Reasons that mean the account's plan does not offer this model. */
const NOT_OFFERED = new Set(['account_model_not_discovered', 'discovered_model_unavailable']);

/**
 * A row-level state: limited for this model only, the account the next turn runs on signed out,
 * or not offered on that account. `account` is named only where the person has several (a vendor with one shows none).
 */
export function rowNote(model: Model, account?: VendorAccount | null): UsageSegment | null {
	const turn = modelAvailability(model);
	if (turn.status !== 'unavailable') return null;
	if (turn.status_reason === 'missing_context_window') return { text: 'no context window', level: 'normal' };
	if (turn.status_reason === 'usage_limited') {
		const back = resetLabel(turn.resets_at, false);
		return { text: back ? `limited · back ${back}` : 'limited', level: 'red' };
	}
	if (turn.status_reason === 'account_signed_out') return { text: 'signed out', level: 'normal' };
	if (account && turn.status_reason && NOT_OFFERED.has(turn.status_reason)) {
		return { text: `not offered on ${account.label.name}`, level: 'normal' };
	}
	return { text: 'unavailable', level: 'normal' };
}
