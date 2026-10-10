import { describe, expect, it } from 'vitest';

import { resetLabel } from './modelGroups';
import type { Model } from './models';
import type { RunsOn } from './modelTurn';
import { limitHeldLine, PLAN_LIMIT_REASON } from './overageSetting';
import type { VendorAccount } from './vendorAccounts';

// The held line names accounts: which one is at its limit, and where to continue.
const now = new Date('2026-10-06T09:00:00');
const FIRST = new Date('2026-10-08T16:37:00').toISOString();
const SECOND = new Date('2026-10-07T18:37:00').toISOString();

function account(id: string, text: string, provider = 'claude-code', over: Partial<VendorAccount> = {}): VendorAccount {
	return {
		id, provider, name: id, is_default: false, state: 'signed_in', revision: 1, identity: {},
		label: { name: text.split(' · ')[0], email: null, text, custom: null },
		...over,
	};
}

const limitedEntry = (accountId: string, resets: string | null, over: Partial<RunsOn> = {}): RunsOn => ({
	account_id: accountId, selected: false, status: 'unavailable', status_reason: 'usage_limited', resets_at: resets,
	usage: { state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: resets, windows: [] }, ...over,
});
const readyEntry = (accountId: string): RunsOn => ({
	account_id: accountId, selected: false, status: 'ready', status_reason: null, resets_at: null,
	usage: { state: 'allowed', usable: true, windows: [] },
});
const signedOutEntry = (accountId: string): RunsOn => ({
	account_id: accountId, selected: false, status: 'unavailable', status_reason: 'connection_unavailable', resets_at: null,
	usage: { state: 'unknown', usable: false, windows: [] },
});

function model(entries: RunsOn[], connection = 'claude-code'): Model {
	return {
		id: `${connection}/opus`, name: 'Opus', connection, size: '', backend: 'harness', speed: '',
		status: 'ready', runs_on: entries,
	} as Model;
}

const PERSONAL = account('personal', 'Personal Max · sam@example.com');
const ACME = account('acme', 'Acme (Team) · sam@example.com');
const THIRD = account('third', 'Studio · sam@studio.example.com');
const ACCOUNTS = [PERSONAL, ACME, THIRD];
const back = (iso: string) => resetLabel(iso, true, now);

describe('limitHeldLine with accounts: another account is ready', () => {
	const bound = limitedEntry('personal', FIRST, { selected: true });

	it('names the limited account and offers one move to the ready one', () => {
		const line = limitHeldLine(model([bound, readyEntry('acme')]), now, { accounts: ACCOUNTS });
		expect(back(FIRST)).not.toBe('');
		expect(line).toEqual({
			text: `Personal Max · sam@example.com is at its limit · back ${back(FIRST)}`,
			planLimit: false,
			action: { accountId: 'acme', label: 'Continue on Acme (Team) · sam@example.com' },
		});
	});

	it('picks the first ready account in the picker order, not a second ordering', () => {
		const row = model([bound, readyEntry('acme'), readyEntry('third')]);
		expect(limitHeldLine(row, now, { accounts: ACCOUNTS })?.action?.accountId).toBe('acme');
		// Rollover on: the policy's order wins, exactly as the picker rows are ordered.
		const policy = { rollover: true, order: ['third', 'personal', 'acme'] };
		expect(limitHeldLine(row, now, { accounts: ACCOUNTS, policy })?.action?.accountId).toBe('third');
	});

	it('skips an account the catalog does not mark ready', () => {
		const row = model([bound, signedOutEntry('acme'), readyEntry('third')]);
		expect(limitHeldLine(row, now, { accounts: ACCOUNTS })?.action)
			.toEqual({ accountId: 'third', label: 'Continue on Studio · sam@studio.example.com' });
	});

	it('reads the picker tail when the reset time is unknown, and still offers the move', () => {
		const row = model([limitedEntry('personal', null, { selected: true }), readyEntry('acme')]);
		const line = limitHeldLine(row, now, { accounts: ACCOUNTS });
		expect(line?.text).toBe('Personal Max · sam@example.com is at its limit · reset time unknown');
		expect(line?.action?.accountId).toBe('acme');
	});
});

describe('limitHeldLine with accounts: every account is limited', () => {
	it.each([['claude-code', 'Claude'], ['codex', 'Codex']])('says so for %s, first back the earliest reset, with no action', (connection, vendor) => {
		const row = model([limitedEntry('personal', FIRST, { selected: true }), limitedEntry('acme', SECOND)], connection);
		const line = limitHeldLine(row, now, { accounts: ACCOUNTS });
		expect(back(SECOND)).not.toBe(back(FIRST));
		expect(line).toEqual({
			text: `All ${vendor} accounts are at their limit · first back ${back(SECOND)}`,
			planLimit: false,
			allLimited: true,
		});
	});

	it('needs no account labels, and reads the picker tail when no reset is known', () => {
		const row = model([limitedEntry('personal', null, { selected: true }), limitedEntry('acme', null)]);
		expect(limitHeldLine(row, now)?.text).toBe('All Claude accounts are at their limit · reset time unknown');
	});
});

describe('limitHeldLine keeps today\'s line where the design says nothing', () => {
	const today = (reset: string | null, plan = false) =>
		`${plan ? 'Plan' : 'Usage'} limit reached · ${reset ? `resets ${back(reset)}` : 'reset time unknown'}`;
	const ctx = { accounts: ACCOUNTS };

	it('a vendor with one account', () => {
		const line = limitHeldLine(model([limitedEntry('personal', FIRST, { selected: true })]), now, ctx);
		expect(line).toEqual({ text: today(FIRST), planLimit: false });
	});

	it('a plan limit, whatever the other accounts can do', () => {
		const planLimited = limitedEntry('personal', FIRST, {
			selected: true,
			usage: { state: 'limited', usable: false, reason: PLAN_LIMIT_REASON, resets_at: FIRST, windows: [] },
		});
		const line = limitHeldLine(model([planLimited, readyEntry('acme')]), now, ctx);
		expect(line).toEqual({ text: today(FIRST, true), planLimit: true });
	});

	it('the other accounts signed out or not offered', () => {
		const notOffered = { ...signedOutEntry('acme'), status_reason: 'account_model_not_discovered' };
		const line = limitHeldLine(model([limitedEntry('personal', FIRST, { selected: true }), notOffered, signedOutEntry('third')]), now, ctx);
		expect(line).toEqual({ text: today(FIRST), planLimit: false });
	});

	it('some accounts limited and the rest not ready, so not all limited and none to move to', () => {
		const line = limitHeldLine(model([limitedEntry('personal', FIRST, { selected: true }), limitedEntry('acme', SECOND), signedOutEntry('third')]), now, ctx);
		expect(line).toEqual({ text: today(FIRST), planLimit: false });
	});

	it('accounts that are not loaded, or no thread to move', () => {
		const row = model([limitedEntry('personal', FIRST, { selected: true }), readyEntry('acme')]);
		expect(limitHeldLine(row, now, { accounts: [] })).toEqual({ text: today(FIRST), planLimit: false });
		expect(limitHeldLine(row, now)).toEqual({ text: today(FIRST), planLimit: false });
	});
});
