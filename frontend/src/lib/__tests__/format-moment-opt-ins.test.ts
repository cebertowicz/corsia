import { describe, expect, it } from 'vitest';
import { formatMoment } from '$lib/time';

// Local wall-clock moments, so the test holds in any runner time zone.
const NOW = new Date(2026, 8, 25, 10, 0);
const at = (days: number, hour: number, minute = 0) => new Date(2026, 8, 25 + days, hour, minute);
const clock = (date: Date) => date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const shortDate = (date: Date) => date.toLocaleDateString([], { month: 'short', day: 'numeric' });

describe('formatMoment opt-ins for the Scheduled group', () => {
	it('keeps the default exactly as it was', () => {
		expect(formatMoment(at(20, 9), NOW)).toBe(shortDate(at(20, 9)));
		expect(formatMoment(at(1, 9), NOW)).toBe(`tomorrow at ${clock(at(1, 9))}`);
		expect(formatMoment(at(20, 9), NOW, { zone: false })).toBe(shortDate(at(20, 9)));
	});

	it('clock: a far date keeps its clock and names no zone', () => {
		expect(formatMoment(at(20, 9), NOW, { clock: true })).toBe(`${shortDate(at(20, 9))} at ${clock(at(20, 9))}`);
		expect(formatMoment(at(-3, 9), NOW, { clock: true })).toBe(`${shortDate(at(-3, 9))} at ${clock(at(-3, 9))}`);
		expect(formatMoment(at(1, 9), NOW, { clock: true })).toBe(`tomorrow at ${clock(at(1, 9))}`);
	});

	it('plain: joins day and clock with a space, for the cluster line', () => {
		const monday = at(4, 9);
		expect(formatMoment(monday, NOW, { clock: true, plain: true }))
			.toBe(`${monday.toLocaleDateString([], { weekday: 'long' })} ${clock(monday)}`);
		expect(formatMoment(at(1, 9), NOW, { clock: true, plain: true })).toBe(`tomorrow ${clock(at(1, 9))}`);
		expect(formatMoment(at(20, 9), NOW, { clock: true, plain: true })).toBe(`${shortDate(at(20, 9))} ${clock(at(20, 9))}`);
	});
});
