import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { accountSegments, groupModels, groupStatusSegments, resetLabel, rowNote, usageBreakdown, windowLabel, windowReading, type ModelGroup } from './modelGroups';
import type { Model } from './models';
import { withAccountTurn, type TurnFields } from './modelFixtures';
import type { UsageWindow } from './modelUsage';
import type { VendorAccount, VendorAccountUsage } from './vendorAccounts';

// Every instant is built from local-time constructors so 'today', 'within six days' and 'later date'
// hold in any zone; the clock is pinned for the helpers that read it by default.
const NOW = new Date(2026, 8, 30, 12, 0);
const localIso = (dayOffset: number, hour: number) => new Date(2026, 8, 30 + dayOffset, hour, 0).toISOString();

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());
const WEEKDAY = /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)$/;

function groupStatusText(group: ModelGroup, accounts: VendorAccount[] = []): string {
	return groupStatusSegments(group, accounts).map((segment) => segment.text).join(' · ');
}

/** A signed-in account as `vendorAccounts` delivers it; the usage fields default to a settled read. */
function account(windows: UsageWindow[] | null, over: Partial<VendorAccount> = {}, usage: Partial<VendorAccountUsage> = {}): VendorAccount {
	return {
		id: 'acct-1', provider: 'claude-code', name: 'Work', is_default: false, state: 'signed_in', revision: 1, identity: {},
		label: { name: 'Work', email: 'work@example.test', text: 'Work', custom: null },
		usage: windows === null ? undefined : { state: 'allowed', usable: true, windows, checking: false, check: null, ...usage },
		...over,
	};
}
const limitedAccount = (usage: Partial<VendorAccountUsage> = {}) => account([], {}, { state: 'limited', usable: false, resets_at: '2026-10-03T05:00:00Z', ...usage });

function win(over: Partial<UsageWindow>): UsageWindow {
	return { kind: 'seven_day', state: 'reported', used_percent: 10, resets_at: '2026-10-03T05:00:00Z', ...over };
}

function row(over: Partial<Model> & TurnFields): Model {
	return withAccountTurn({
		id: 'm', name: 'M', size: '', backend: 'cc', provider: 'claude-code', speed: '',
		status: 'ready', connection: 'claude-code', available: true, ...over,
	});
}

describe('windows', () => {
	it('names a window from its length, else its kind', () => {
		expect(windowLabel(win({ window_minutes: 10080 }))).toBe('wk');
		expect(windowLabel(win({ window_minutes: 300 }))).toBe('5h');
		expect(windowLabel(win({ kind: 'five_hour' }))).toBe('5h');
		expect(windowLabel(win({ kind: 'seven_day' }))).toBe('wk');
		expect(windowLabel(win({ kind: 'monthly_credits' }))).toBe('monthly credits');
	});

	it('labels a reset as a time today, a weekday within six days, else a date', () => {
		expect(resetLabel(localIso(0, 14), false, NOW)).toMatch(/\d/);
		expect(resetLabel(localIso(3, 5), false, NOW)).toMatch(WEEKDAY);
		expect(resetLabel(localIso(3, 5), true, NOW)).toMatch(/^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d/);
		expect(resetLabel(localIso(20, 5), false, NOW)).toMatch(/Oct/);
		expect(resetLabel(null)).toBe('');
	});
});

describe('usage breakdown line', () => {
	it('pairs both numbers with the reset of the window the percentage was read from', () => {
		const window: UsageWindow = {
			kind: 'five_hour', state: 'reported', used_percent: 62, window_minutes: 300,
			resets_at: localIso(80, 12), used_percent_resets_at: localIso(61, 12),
		};
		const [line] = usageBreakdown(account([window]));
		expect(line).toContain(`5h · 38% left · 62% used · resets ${resetLabel(localIso(61, 12), true)}`);
		expect(line).not.toContain(resetLabel(localIso(80, 12), true));
	});

	it('omits the reset when the window carries none', () => {
		expect(usageBreakdown(account([win({ used_percent: 42, window_minutes: 300, resets_at: null })]))).toEqual(['5h · 58% left · 42% used']);
	});
});

describe('windowReading', () => {
	// raw used -> [left, used, level]: left rounds down, used rounds up and holds at 99 until spent,
	// and the level reads the raw value, never the rounded one.
	const table: Array<[number, number, number, 'normal' | 'amber' | 'red']> = [
		[0, 100, 0, 'normal'],
		[42, 58, 42, 'normal'],
		[42.4, 57, 43, 'normal'],
		[74.99, 25, 75, 'normal'],
		[75, 25, 75, 'amber'],
		[89.99, 10, 90, 'amber'],
		[90, 10, 90, 'red'],
		[99, 1, 99, 'red'],
		[99.5, 0, 99, 'red'],
		[100, 0, 100, 'red'],
	];
	it.each(table)('reads %s%% used', (raw, left, used, level) => {
		expect(windowReading(raw)).toEqual({ left, used, level });
	});

	it('clamps a value outside 0 to 100', () => {
		expect(windowReading(-5)).toEqual({ left: 100, used: 0, level: 'normal' });
		expect(windowReading(130)).toEqual({ left: 0, used: 100, level: 'red' });
	});

	it('never shows a window that is not spent as 0% left or 100% used', () => {
		for (const raw of [99, 99.01, 99.5, 99.99, 99.999]) {
			const reading = windowReading(raw);
			expect(reading.used).toBeLessThan(100);
			const [segment] = accountSegments(account([win({ window_minutes: 10080, used_percent: raw })]));
			expect(segment.text).not.toContain('0% left');
			expect(segment.text).toMatch(/^wk (<1|1)% left$/);
		}
	});
});

describe('usage text by window state', () => {
	const acct = (windows: UsageWindow[]) => account(windows);
	const accountText = (windows: UsageWindow[]) => accountSegments(acct(windows)).map((segment) => segment.text).join(' · ');
	const five = (used: number) => win({ kind: 'five_hour', window_minutes: 300, used_percent: used });
	const weekly = (used: number) => win({ kind: 'seven_day', window_minutes: 10080, used_percent: used });
	const cases: Array<[number, string, 'normal' | 'amber' | 'red', string]> = [
		[0, '5h 100% left', 'normal', '5h · 100% left · 0% used'],
		[42.4, '5h 57% left', 'normal', '5h · 57% left · 43% used'],
		[74.99, '5h 25% left', 'normal', '5h · 25% left · 75% used'],
		[75, '5h 25% left', 'amber', '5h · 25% left · 75% used'],
		[89.99, '5h 10% left', 'amber', '5h · 10% left · 90% used'],
		[90, '5h 10% left', 'red', '5h · 10% left · 90% used'],
		[99.5, '5h <1% left', 'red', '5h · <1% left · 99% used'],
		[100, '5h 0% left', 'red', '5h · 0% left · 100% used'],
	];

	it.each(cases)('at %s%% used the account reads %s (%s) and the breakdown both numbers', (used, text, level, line) => {
		const a = acct([five(used)]);
		expect(accountSegments(a)).toEqual([{ text, level, label: expect.stringMatching(/^5-hour: /) }]);
		expect(usageBreakdown(a)[0].startsWith(line)).toBe(true);
	});

	it('spells every window out for assistive technology', () => {
		const [a, b] = accountSegments(acct([five(42), weekly(94)]));
		expect(a.label).toBe('5-hour: 58% left');
		expect(b.label).toBe('weekly: 6% left');
		expect(accountSegments(acct([weekly(99.5)]))[0].label).toBe('weekly: less than 1% left');
	});

	it('colors each segment on its own', () => {
		expect(accountSegments(acct([weekly(94), five(42)])).map((s) => [s.text, s.level])).toEqual([
			['5h 58% left', 'normal'],
			['wk 6% left', 'red'],
		]);
		expect(accountSegments(acct([five(80), weekly(92)])).map((s) => s.level)).toEqual(['amber', 'red']);
		expect(accountText([five(42), weekly(94)])).toBe('5h 58% left · wk 6% left');
	});

	it('reads a stale window as usage unknown at normal level, in the segments and the breakdown', () => {
		const stale = acct([win({ kind: 'five_hour', window_minutes: 300, used_percent: 88, state: 'unknown' })]);
		expect(accountSegments(stale)).toEqual([{ text: 'usage unknown', level: 'normal' }]);
		expect(usageBreakdown(stale)).toEqual(['5h · usage unknown']);
		const mixed = acct([five(42), win({ kind: 'seven_day', window_minutes: 10080, used_percent: 88, state: 'unknown' })]);
		expect(accountSegments(mixed).map((s) => s.text)).toEqual(['5h 58% left']);
		expect(usageBreakdown(mixed)[1]).toBe('wk · usage unknown');
	});

	it('shows a limited account in red in all three variants, and a signed-out one at normal level', () => {
		const checking = accountSegments(limitedAccount({ checking: true }));
		const completed = accountSegments(limitedAccount({ checking: true, check: { outcome: 'limited', at: '2026-10-06T16:00:00Z' } }));
		const failed = accountSegments(limitedAccount({ check: { outcome: 'failed', error: 'offline' } }));
		const back = accountSegments(limitedAccount());
		for (const segments of [checking, failed, back]) {
			expect(segments).toHaveLength(1);
			expect(segments[0].level).toBe('red');
		}
		expect(checking[0].text).toBe('limited · checking');
		expect(completed[0].text).toMatch(/^limited · back /);
		expect(failed[0].text).toMatch(/^limited · back .* · couldn't verify$/);
		expect(back[0].text).toMatch(/^limited · back /);
		expect(accountSegments(account(null, { state: 'signed_out' }))).toEqual([{ text: 'signed out', level: 'normal' }]);
	});

	it('reads usage unknown for an account with no usage', () => {
		expect(accountSegments(account(null))).toEqual([{ text: 'usage unknown', level: 'normal' }]);
	});

	it('reads the same text and level for Claude Code and Codex given the same numbers', () => {
		const windows = [five(42), weekly(80)];
		const claude = accountSegments(account(windows));
		const codex = accountSegments(account(windows, { provider: 'codex' }));
		expect(codex).toEqual(claude);
		expect(claude.map((s) => [s.text, s.level])).toEqual([['5h 58% left', 'normal'], ['wk 20% left', 'amber']]);
		expect(usageBreakdown(account(windows, { provider: 'codex' }))).toEqual(usageBreakdown(account(windows)));
	});

	it('shows paid overage as an amber selectable account state for Codex and Claude', () => {
		const spent = [weekly(100)];
		const codex = account(spent, { provider: 'codex' }, {
			allow_overage: true, facts: { credits: { hasCredits: true } },
		});
		const claude = account(spent, {}, {
			allow_overage: true, facts: { extra_usage: { is_enabled: true, utilization: 20 } },
		});
		expect(accountSegments(codex)).toEqual([{ text: 'plan used · spending credits', level: 'amber' }]);
		expect(accountSegments(claude)).toEqual([{ text: 'plan used · spending extra usage', level: 'amber' }]);
	});

	it('ignores a window scoped to one model', () => {
		const scoped = win({ kind: 'seven_day_opus', scope_model: 'opus', window_minutes: 10080, used_percent: 92 });
		expect(accountText([five(42), scoped])).toBe('5h 58% left');
		expect(usageBreakdown(acct([five(42), scoped]))).toHaveLength(1);
	});
});

describe('groupModels', () => {
	const ready = (over: Partial<Model> & TurnFields = {}) => row({ usage: { state: 'allowed', usable: true, windows: [] }, ...over });

	it('makes one group per connection and shows a single account\'s figures once, on the header', () => {
		const groups = groupModels([
			ready({ id: 'a' }),
			ready({ id: 'b' }),
			ready({ id: 'c', connection: 'codex', provider: 'codex' }),
		]);
		expect(groups.map((g) => [g.name, g.ready.length])).toEqual([['Claude Code', 2], ['Codex', 1]]);
		expect(groupStatusText(groups[0], [account([win({ used_percent: 71 })])])).toBe('wk 29% left');
		expect(groupStatusText(groups[0])).toBe('');
	});

	it('leaves the header of a ready group blank when its vendor has several accounts', () => {
		const [group] = groupModels([ready()]);
		const accounts = [account([win({ used_percent: 71 })]), account([win({ used_percent: 10 })], { id: 'acct-2' })];
		expect(groupStatusSegments(group, accounts)).toEqual([]);
	});

	it('uses the Settings discovery predicate to omit rows returned on no account', () => {
		const rows = [
			row({ id: 'cc/claude-sonnet-5-5', litellm_model: 'claude-code/claude-sonnet-5-5', resolved_identity: 'claude-sonnet-5-5', status: 'unavailable', status_reason: 'not_returned_by_discovery', available: false }),
			row({ id: 'cc/other', litellm_model: 'claude-code/other', resolved_identity: 'other-model', status: 'unavailable', status_reason: 'account_model_not_discovered', available: true }),
		];
		const [group] = groupModels(rows);
		expect(group.rows.map((model) => model.id)).toEqual(['cc/other']);
		expect(group.blocked.map((model) => model.id)).toEqual(['cc/other']);
		const [pinned] = groupModels(rows, 'claude-code/claude-sonnet-5-5');
		expect(pinned.blocked.map((model) => model.id)).toContain('cc/claude-sonnet-5-5');
		const [unknown] = groupModels([row({ id: 'cc/x', litellm_model: 'claude-code/x', status: 'unavailable', status_reason: 'not_returned_by_discovery', available: undefined })]);
		expect(unknown).toBeUndefined();
	});

	it('lists every offered row in catalog order, ready and blocked alike', () => {
		const [group] = groupModels([
			row({ id: 'a' }),
			row({ id: 'b', status: 'unavailable', status_reason: 'usage_limited' }),
			row({ id: 'c' }),
		]);
		expect(group.rows.map((model) => model.id)).toEqual(['a', 'b', 'c']);
	});

	it('suppresses a stale blocked row only within the group that has a ready identity match', () => {
		const groups = groupModels([
			row({ id: 'claude-ready', resolved_identity: 'shared', connection: 'claude-code', provider: 'claude-code' }),
			row({ id: 'claude-stale', resolved_identity: 'shared', connection: 'claude-code', provider: 'claude-code', status: 'unavailable', status_reason: 'usage_limited' }),
			row({ id: 'codex-blocked', resolved_identity: 'shared', connection: 'codex', provider: 'codex', status: 'unavailable', status_reason: 'usage_limited' }),
		]);
		expect(groups.map((group) => [group.name, group.state, group.rows.map((model) => model.id)]))
			.toEqual([
				['Claude Code', 'ready', ['claude-ready']],
				['Codex', 'limited', ['codex-blocked']],
			]);
	});

	it('shows every reported window, shortest first, and nothing for a local group', () => {
		const five = win({ kind: 'five_hour', window_minutes: 300, used_percent: 12 });
		const weekly = win({ kind: 'seven_day', window_minutes: 10080, used_percent: 55 });
		const text = (windows: UsageWindow[]) => accountSegments(account(windows)).map((segment) => segment.text).join(' · ');
		expect(text([weekly, five])).toBe('5h 88% left · wk 45% left');
		expect(text([weekly])).toBe('wk 45% left');
		expect(text([five, win({ kind: 'seven_day', used_percent: 90, state: 'unknown' })])).toBe('5h 88% left');
		expect(text([win({ state: 'unknown' })])).toBe('usage unknown');
		const [local] = groupModels([row({ id: 'l', connection: 'omlx', provider: 'omlx', backend: 'network', server_name: 'This Mac' })]);
		expect(groupStatusText(local)).toBe('');
		expect(usageBreakdown(account([weekly, five]))).toEqual([
			expect.stringMatching(/^5h · 88% left · 12% used · resets /),
			expect.stringMatching(/^wk · 45% left · 55% used · resets /),
		]);
	});

	describe('an account never shows the same label twice', () => {
		// Windows as `/api/vendor-accounts` projects them: plan windows only, a model-tier window carrying its tier.
		const codex = (name: 'primary' | 'secondary', minutes: number, used: number) =>
			win({ kind: `codex_${name}`, window_minutes: minutes, used_percent: used });
		const header = (windows: UsageWindow[]) => {
			const texts = accountSegments(account(windows)).map((segment) => segment.text);
			const labels = texts.map((text) => text.split(' ')[0]);
			expect(new Set(labels).size).toBe(labels.length);
			return texts;
		};

		it('Codex with a primary window only', () => {
			expect(header([codex('primary', 10080, 7)])).toEqual(['wk 93% left']);
		});

		it('Codex with primary and secondary windows of different lengths', () => {
			expect(header([codex('secondary', 10080, 7), codex('primary', 300, 40)])).toEqual(['5h 60% left', 'wk 93% left']);
		});

		it('Codex with primary and secondary windows of the same length: the one closer to its limit', () => {
			const windows = [codex('primary', 10080, 6), codex('secondary', 10080, 7)];
			expect(header(windows)).toEqual(['wk 93% left']);
			expect(header([...windows].reverse())).toEqual(['wk 93% left']);
			expect(usageBreakdown(account(windows))).toHaveLength(2);
		});

		it('Codex after a full read with by-limit buckets and a live update: the one plan window the backend projects', () => {
			// backend/tests/test_usage_one_meter.py proves the projection holds one window for this payload.
			expect(header([{ ...codex('primary', 10080, 7), source: 'rate_limit_update' }])).toEqual(['wk 93% left']);
		});

		it('Claude with 5-hour, weekly and a model-tier window', () => {
			const windows = [
				win({ kind: 'seven_day_sonnet', used_percent: 80, scope_model: 'sonnet' }),
				win({ kind: 'seven_day', used_percent: 45 }),
				win({ kind: 'five_hour', used_percent: 12 }),
			];
			expect(header(windows)).toEqual(['5h 88% left', 'wk 55% left']);
		});

		it('shows each of three Claude accounts its refreshed 5-hour and weekly picker segments', () => {
			const accounts = [
				account([win({ kind: 'five_hour', window_minutes: 300, used_percent: 63 }), win({ used_percent: 46 })]),
				account([win({ kind: 'five_hour', window_minutes: 300, used_percent: 15 }), win({ used_percent: 25 })], { id: 'acct-2' }),
				account([win({ kind: 'five_hour', window_minutes: 300, used_percent: 80 }), win({ used_percent: 90 })], { id: 'acct-3' }),
			];
			expect(accounts.map((item) => accountSegments(item).map((segment) => segment.text))).toEqual([
				['5h 37% left', 'wk 54% left'],
				['5h 85% left', 'wk 75% left'],
				['5h 20% left', 'wk 10% left'],
			]);
		});
	});

	it('reads a stale window as unknown, never a remembered percentage', () => {
		const stale = account([win({ used_percent: 88, state: 'unknown' })]);
		expect(accountSegments(stale).map((segment) => segment.text)).toEqual(['usage unknown']);
		expect(usageBreakdown(stale)).toEqual(['wk · usage unknown']);
	});

	it('gives a local server no meter', () => {
		const [group] = groupModels([row({ id: 'l', connection: 'omlx', provider: 'omlx', backend: 'network', server_name: 'This Mac' })]);
		expect(group.name).toBe('This Mac');
		expect(groupStatusText(group)).toBe('');
	});

	it('uses first back only when several accounts are limited', () => {
		const [group] = groupModels([
			row({ id: 'a', status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2026-10-03T05:00:00Z' }),
			row({ id: 'b', status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2026-10-03T05:00:00Z' }),
		]);
		expect(group.state).toBe('limited');
		expect(group.ready).toHaveLength(0);
		expect(groupStatusText(group, [limitedAccount()])).toMatch(/^limited · back (Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d/);
		expect(groupStatusText(group, [limitedAccount()])).not.toContain('first back');
		expect(groupStatusText(group, [limitedAccount(), limitedAccount({ resets_at: '2026-10-04T05:00:00Z' })])).toMatch(/^All 2 accounts limited · first back (Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d/);
	});

	it('reports an account check in flight or failed, from the account', () => {
		const [group] = groupModels([row({ status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2026-10-03T05:00:00Z' })]);
		expect(groupStatusText(group, [limitedAccount({ checking: true })])).toBe('limited · checking');
		expect(groupStatusText(group, [limitedAccount({ check: { outcome: 'failed', error: 'offline' } })])).toMatch(/^limited · back .* · couldn't verify$/);
		expect(groupStatusText(group, [limitedAccount({ check: { outcome: 'failed', error: 'offline' } }), limitedAccount({ check: { outcome: 'failed', error: 'offline' } })])).toMatch(/^All 2 accounts limited · first back .* · couldn't verify$/);
		const [groupWithoutReset] = groupModels([row({ status: 'unavailable', status_reason: 'usage_limited' })]);
		expect(groupStatusText(groupWithoutReset, [limitedAccount({ resets_at: null, check: { outcome: 'failed', error: 'offline' } })])).toBe("limited · couldn't verify");
		expect(groupStatusText(groupWithoutReset)).toBe('limited');
	});

	it('reads signed out from the rows', () => {
		const [group] = groupModels([row({ status: 'unavailable', status_reason: 'connection_unavailable' })]);
		expect(group.state).toBe('signed_out');
		expect(groupStatusText(group)).toBe('signed out');
	});

	it('omits rows that are not ready and carry no reason, and aliases of offered rows', () => {
		expect(groupModels([
			row({ id: 'off', status: 'unavailable' }),
			row({ id: 'unloaded', status: 'available' }),
		])).toEqual([]);
		const groups = groupModels([row({ id: 'a' }), row({ id: 'alias', alias_of: 'a' })]);
		expect(groups[0].ready.map((m) => m.id)).toEqual(['a']);
		expect(groupModels([row({ id: 'a' }), row({ id: 'alias', alias_of: 'a' })], 'alias')[0].ready).toHaveLength(2);
	});

	it('notes a model limited on its own, one with no context window, and one the marked account does not offer', () => {
		const [group] = groupModels([
			row({ id: 'opus' }),
			row({ id: 'gone', status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2026-10-03T05:00:00Z' }),
			row({ id: 'noctx', status: 'unavailable', status_reason: 'missing_context_window' }),
			row({ id: 'absent', status: 'unavailable', status_reason: 'account_model_not_discovered' }),
			row({ id: 'dropped', status: 'unavailable', status_reason: 'discovered_model_unavailable' }),
		]);
		expect(group.state).toBe('ready');
		const [opus, gone, noctx, absent, dropped] = group.rows;
		const marked = account([], { label: { name: 'Personal', email: null, text: 'Personal', custom: null } });
		expect(rowNote(opus, marked)).toBeNull();
		expect(rowNote(gone, marked)).toEqual({ text: expect.stringMatching(/^limited · back /), level: 'red' });
		expect(rowNote(noctx, marked)).toEqual({ text: 'no context window', level: 'normal' });
		expect(rowNote(absent, marked)).toEqual({ text: 'not offered on Personal', level: 'normal' });
		expect(rowNote(dropped, marked)).toEqual({ text: 'not offered on Personal', level: 'normal' });
		// With no marked account (a vendor with one account) nothing names an account.
		expect(rowNote(absent)).toEqual({ text: 'unavailable', level: 'normal' });
	});
});
