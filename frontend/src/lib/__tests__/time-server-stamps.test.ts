import { afterEach, describe, expect, it, vi } from 'vitest';

import { inZone } from '$lib/__tests__/fixtures/focusRuns';
import { compareMessageOrder } from '$lib/messageHistory';
import { bucketOf, formatDateTime, formatTime, parseUTC, parseWsTimestamp, relLabel } from '$lib/time';

const ZONES = ['America/Los_Angeles', 'Asia/Kolkata', 'UTC'];

afterEach(() => vi.useRealTimers());

describe('parseUTC', () => {
	it('reads a naive stamp as UTC, exactly as it did before', () => {
		for (const zone of ZONES) {
			inZone(zone, () => {
				for (const naive of ['2026-10-02T04:00:00', '2026-10-02T04:00:00.123456', '2026-03-08T10:00:00']) {
					expect(parseUTC(naive).getTime()).toBe(new Date(`${naive.slice(0, 23)}Z`).getTime());
				}
			});
		}
	});

	it('keeps an explicit zone instead of reading the wall clock as UTC', () => {
		for (const zone of ZONES) {
			inZone(zone, () => {
				expect(parseUTC('2026-10-02T04:00:00Z').toISOString()).toBe('2026-10-02T04:00:00.000Z');
				expect(parseUTC('2026-10-02T04:00:00+00:00').toISOString()).toBe('2026-10-02T04:00:00.000Z');
				expect(parseUTC('2026-10-02T04:00:00+02:00').toISOString()).toBe('2026-10-02T02:00:00.000Z');
				expect(parseUTC('2026-10-02T04:00:00-07:00').toISOString()).toBe('2026-10-02T11:00:00.000Z');
			});
		}
	});

	it('gives an invalid date for what is not a stamp', () => {
		expect(Number.isNaN(parseUTC('soon').getTime())).toBe(true);
		expect(Number.isNaN(parseUTC('').getTime())).toBe(true);
		expect(Number.isNaN(parseWsTimestamp(null))).toBe(true);
	});
});

describe('what reads a server stamp through parseUTC', () => {
	it('formatTime shows the same clock for a naive and an explicit-offset stamp, in each zone', () => {
		const clocks: Record<string, string> = { 'America/Los_Angeles': '09:00 PM', 'Asia/Kolkata': '09:30 AM', UTC: '04:00 AM' };
		for (const zone of ZONES) {
			inZone(zone, () => {
				expect(formatTime('2026-10-02T04:00:00')).toBe(clocks[zone]);
				expect(formatTime('2026-10-02T04:00:00+00:00')).toBe(clocks[zone]);
				expect(formatTime('2026-10-02T06:00:00+02:00')).toBe(clocks[zone]);
			});
		}
	});

	it('formatTime follows both daylight-saving changes in Pacific time and ignores them in a zone without one', () => {
		inZone('America/Los_Angeles', () => {
			// Clocks go forward: 1:59 AM PST is followed by 3:00 AM PDT.
			expect(formatTime('2026-03-08T09:59:00')).toBe('01:59 AM');
			expect(formatTime('2026-03-08T10:00:00+00:00')).toBe('03:00 AM');
			// Clocks go back: 1:59 AM PDT is followed by 1:00 AM PST.
			expect(formatTime('2026-11-01T08:59:00')).toBe('01:59 AM');
			expect(formatTime('2026-11-01T09:00:00+00:00')).toBe('01:00 AM');
		});
		inZone('Asia/Kolkata', () => {
			expect(formatTime('2026-03-08T09:59:00')).toBe('03:29 PM');
			expect(formatTime('2026-03-08T10:00:00+00:00')).toBe('03:30 PM');
			expect(formatTime('2026-11-01T08:59:00')).toBe('02:29 PM');
			expect(formatTime('2026-11-01T09:00:00+00:00')).toBe('02:30 PM');
		});
	});

	it('bucketOf places a naive and an explicit-offset stamp of one instant in the same bucket', () => {
		for (const zone of ZONES) {
			inZone(zone, () => {
				const now = new Date(Date.UTC(2026, 9, 2, 20, 0, 0));
				for (const [naive, offset] of [
					['2026-10-02T19:00:00', '2026-10-02T21:00:00+02:00'],
					['2026-09-30T19:00:00', '2026-09-30T12:00:00-07:00'],
					['2026-08-01T00:00:00', '2026-08-01T00:00:00+00:00'],
				]) {
					expect(bucketOf(offset, now)).toBe(bucketOf(naive, now));
				}
				expect(bucketOf('2026-10-02T19:00:00', now)).toBe('Today');
				expect(bucketOf('2026-08-01T00:00:00', now)).toBe('Older');
			});
		}
	});

	it('formatDateTime shows one instant the same way in either form', () => {
		for (const zone of ZONES) {
			inZone(zone, () => {
				expect(formatDateTime('2026-10-02T06:00:00+02:00')).toBe(formatDateTime('2026-10-02T04:00:00'));
				expect(formatDateTime('2026-10-02T04:00:00')).toBe(new Date('2026-10-02T04:00:00Z').toLocaleString());
			});
		}
	});

	it('relLabel measures from the instant, not from the wall clock of an offset stamp', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-10-02T04:30:00Z'));

		expect(relLabel('2026-10-02T04:00:00')).toBe('30m ago');
		expect(relLabel('2026-10-02T06:00:00+02:00')).toBe('30m ago');
		expect(relLabel('2026-10-01T21:00:00-07:00')).toBe('30m ago');
	});

	it('message order compares instants across stamp forms', () => {
		const earlier = { id: 'a', created_at: '2026-10-02T06:00:00+02:00' };
		const later = { id: 'b', created_at: '2026-10-02T04:30:00' };

		expect(compareMessageOrder(earlier, later)).toBeLessThan(0);
		expect(compareMessageOrder(later, earlier)).toBeGreaterThan(0);
		expect(compareMessageOrder(earlier, { id: 'c', created_at: '2026-10-02T04:00:00' })).toBe(0);
	});
});
