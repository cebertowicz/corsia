import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { formatMoment, localDayClock, msUntilNextLocalDay } from '$lib/time';
import { localizeInstants, nodeSubline, settledMoment } from '$lib/components/plans/planDisplay';
import { operatorNode } from './fixtures/operatorPlan';

// Local wall-clock moments, so the test holds in any runner time zone.
const NOW = new Date(2026, 8, 25, 10, 0);
const at = (days: number, hour: number, minute = 0) =>
	new Date(2026, 8, 25 + days, hour, minute);
const clock = (date: Date) => date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const zoned = (date: Date) =>
	date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
const shortDate = (date: Date) => date.toLocaleDateString([], { month: 'short', day: 'numeric' });

describe('a moment said in local time', () => {
	it('names the day, then the time, near today', () => {
		expect(formatMoment(at(0, 17, 30), NOW)).toBe(`today at ${clock(at(0, 17, 30))}`);
		expect(formatMoment(at(-1, 21, 5), NOW)).toBe(`yesterday at ${clock(at(-1, 21, 5))}`);
		expect(formatMoment(at(1, 9), NOW)).toBe(`tomorrow at ${clock(at(1, 9))}`);
	});

	it('names the weekday for the coming week and only the date otherwise', () => {
		const friday = at(3, 9);
		expect(formatMoment(friday, NOW)).toBe(
			`${friday.toLocaleDateString([], { weekday: 'long' })} at ${clock(friday)}`,
		);
		expect(formatMoment(at(-3, 9), NOW)).toBe(shortDate(at(-3, 9)));
		expect(formatMoment(at(20, 9), NOW)).toBe(shortDate(at(20, 9)));
		const nextYear = new Date(2027, 0, 5, 9);
		expect(formatMoment(nextYear, NOW)).toBe(
			nextYear.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' }),
		);
	});

	it('turns the day at local midnight, not after 24 hours', () => {
		const justAfterMidnight = new Date(2026, 8, 25, 0, 5);
		expect(formatMoment(at(-1, 23, 59), justAfterMidnight)).toMatch(/^yesterday at /);
		expect(formatMoment(at(0, 0, 0), justAfterMidnight)).toMatch(/^today at /);
	});

	it('names the zone only when asked', () => {
		const later = at(0, 17, 30);
		expect(formatMoment(later, NOW, { zone: true })).toBe(`today at ${zoned(later)}`);
		expect(zoned(later)).toMatch(/[A-Z]{2,5}|GMT|UTC/);
		expect(formatMoment(later, NOW)).toBe(`today at ${clock(later)}`);
	});

	it('keeps the clock and zone on a far date when the zone is asked for', () => {
		for (const far of [at(-2, 9), at(-3, 9), at(7, 9), at(20, 9)]) {
			expect(formatMoment(far, NOW, { zone: true })).toBe(`${shortDate(far)} at ${zoned(far)}`);
			expect(formatMoment(far, NOW)).toBe(shortDate(far));
		}
	});

	it('is empty for an invalid date', () => {
		expect(formatMoment(new Date(Number.NaN), NOW)).toBe('');
	});
});

describe('plan wait times read in local time', () => {
	it('rewrites every instant in a reason and leaves the rest of the text alone', () => {
		const later = at(0, 17, 30);
		expect(localizeInstants(`Waiting until ${later.toISOString()}`, NOW)).toBe(
			`today at ${zoned(later)}`,
		);
		expect(localizeInstants('Waiting for reviewer.verdict', NOW)).toBe('Waiting for reviewer.verdict');
		const reset = localizeInstants('Provider resets at 2026-09-25T17:00:00+00:00', NOW);
		expect(reset).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
		expect(reset.startsWith('Provider resets at ')).toBe(true);
	});

	it('keeps an explicit offset instead of reading it as UTC', () => {
		const instant = '2026-09-25T10:00:00-07:00';
		expect(localizeInstants(instant, NOW)).toBe(formatMoment(new Date(instant), NOW, { zone: true }));
	});

	afterEach(() => vi.useRealTimers());

	it('the row line under a waiting task carries the local time', () => {
		vi.useFakeTimers();
		vi.setSystemTime(NOW);
		const later = at(1, 9);
		const node = operatorNode({ id: 'w', status_reason: `Waiting until ${later.toISOString()}` });
		node.display = { label_key: 'waiting', tone_key: 'waiting' };
		const line = nodeSubline(node) ?? '';
		expect(line).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
		expect(line).toContain(`tomorrow at ${zoned(later)}`);
	});

	it('a scheduled task reads when, then why, from its own deadline', () => {
		vi.useFakeTimers();
		vi.setSystemTime(NOW);
		const due = at(1, 9);
		const node = operatorNode({
			id: 's',
			status_reason: `Until ${due.toISOString().slice(0, 16)}Z · Revisit after the review`,
		});
		node.display = { label_key: 'waiting', tone_key: 'waiting' };
		node.execution = {
			...node.execution,
			condition: 'waiting',
			causes: [{
				kind: 'waiting', reason_key: 'watch', reason: 'ignored',
				owner_kind: 'thread', owner_id: 'agent', ends_at: due.toISOString(),
				scheduled_reason: 'Revisit after the review',
			}],
		};
		expect(nodeSubline(node)).toBe(`tomorrow at ${clock(due)} · Revisit after the review`);
	});

	it('a task with a worker running says why, not the time it is waiting for', () => {
		vi.useFakeTimers();
		vi.setSystemTime(NOW);
		const due = at(0, 14);
		const node = operatorNode({ id: 'r', status_reason: `Waiting until ${due.toISOString()}` });
		node.display = { label_key: 'working', tone_key: 'active' };
		node.execution = {
			...node.execution,
			condition: 'waiting',
			causes: [{
				kind: 'waiting', reason_key: 'watch', reason: 'ignored',
				owner_kind: 'thread', owner_id: 'agent', ends_at: due.toISOString(),
				scheduled_reason: 'Worker is capturing evidence',
			}],
		};
		expect(nodeSubline(node)).toBe('Worker is capturing evidence');
		node.execution = { ...node.execution, causes: [{ ...node.execution.causes[0], scheduled_reason: undefined }] };
		expect(nodeSubline(node) ?? '').not.toMatch(/until|Waiting|today at/i);
	});
});

describe('a settled row says when it settled', () => {
	const done = (when: Date, over = {}) =>
		operatorNode({ id: 'd', state: 'accepted', last_transition_at: when.toISOString(), ...over });

	it('reads today, yesterday, then the date', () => {
		expect(nodeSubline(done(at(0, 8, 40)), NOW)).toBe(`Done · today at ${clock(at(0, 8, 40))}`);
		expect(nodeSubline(done(at(-1, 21, 5)), NOW)).toBe(`Done · yesterday at ${clock(at(-1, 21, 5))}`);
		expect(nodeSubline(done(at(-4, 9)), NOW)).toBe(`Done · ${shortDate(at(-4, 9))}`);
	});

	it('turns yesterday at local midnight', () => {
		const lateLastNight = done(at(-1, 23, 59));
		expect(nodeSubline(lateLastNight, new Date(2026, 8, 24, 23, 59, 30))).toMatch(/^Done · today at /);
		expect(nodeSubline(lateLastNight, new Date(2026, 8, 25, 0, 0, 30))).toMatch(/^Done · yesterday at /);
	});

	it('follows a close reason, and open work carries no moment', () => {
		const closed = done(at(0, 8, 40), {
			state: 'closed', status_reason: 'Not needed after all',
			display: { label_key: 'finished', tone_key: 'closed' },
		});
		expect(nodeSubline(closed, NOW)).toBe(`Won’t do: Not needed after all · today at ${clock(at(0, 8, 40))}`);
		expect(settledMoment(operatorNode({ id: 'open', last_transition_at: at(0, 8).toISOString() }), NOW)).toBeNull();
	});

	it('says who marked a step done, and reads as before without a reason', () => {
		const marked = done(at(0, 15, 40), { status_reason: 'Marked by Infrastructure' });
		expect(nodeSubline(marked, NOW)).toBe(`Done · Marked by Infrastructure · today at ${clock(at(0, 15, 40))}`);
		const followUp = done(at(0, 15, 40), { state: 'accepted_with_followup', status_reason: 'Marked by Infrastructure' });
		expect(nodeSubline(followUp, NOW)).toBe(
			`Done · follow-up created · Marked by Infrastructure · today at ${clock(at(0, 15, 40))}`,
		);
		expect(nodeSubline(done(at(0, 15, 40)), NOW)).toBe(`Done · today at ${clock(at(0, 15, 40))}`);
	});
});

describe('the shared day clock', () => {
	afterEach(() => vi.useRealTimers());

	it('counts to the next local midnight', () => {
		expect(msUntilNextLocalDay(new Date(2026, 8, 25, 23, 59, 0))).toBe(60_000);
	});

	it('ticks once the local day turns, so labels recompute', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date(2026, 8, 25, 23, 59, 0));
		const seen: Date[] = [];
		const stop = localDayClock.subscribe((now) => seen.push(now));
		expect(seen.at(-1)?.getDate()).toBe(25);
		vi.advanceTimersByTime(61_000);
		expect(seen.at(-1)?.getDate()).toBe(26);
		expect(formatMoment(new Date(2026, 8, 25, 23, 30), get(localDayClock))).toMatch(/^yesterday at /);
		stop();
	});
});
