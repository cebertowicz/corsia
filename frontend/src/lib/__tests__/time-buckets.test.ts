import { describe, it, expect } from 'vitest';
import { bucketOf } from '$lib/time';

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const now = new Date(2026, 5, 15, 12, 0, 0);
const ago = (ms: number) => new Date(now.getTime() - ms).toISOString();

describe('bucketOf', () => {
	it('buckets anything from local midnight up to now as Today', () => {
		expect(bucketOf(ago(1 * HOUR), now)).toBe('Today');
		expect(bucketOf(ago(11 * HOUR), now)).toBe('Today');
	});

	it('buckets the prior calendar day as Yesterday', () => {
		expect(bucketOf(ago(25 * HOUR), now)).toBe('Yesterday');
	});

	it('buckets 2–7 days back as This week', () => {
		expect(bucketOf(ago(3 * DAY), now)).toBe('This week');
	});

	it('buckets 8–30 days back as This month', () => {
		expect(bucketOf(ago(10 * DAY), now)).toBe('This month');
	});

	it('buckets beyond 30 days as Older', () => {
		expect(bucketOf(ago(60 * DAY), now)).toBe('Older');
	});

	it('treats invalid / empty input as Older (never throws)', () => {
		expect(bucketOf('', now)).toBe('Older');
		expect(bucketOf('not-a-date', now)).toBe('Older');
	});

	it('parses bare (no-Z) UTC timestamps like the backend emits', () => {
		const bare = ago(2 * HOUR).replace('Z', '');
		expect(bucketOf(bare, now)).toBe('Today');
	});
});
