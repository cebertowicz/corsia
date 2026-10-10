import { describe, expect, it, vi } from 'vitest';
import { formatFreshness, shouldOfferVerification, type DocumentFreshness } from './docFreshness';

function freshness(overrides: Partial<DocumentFreshness> = {}): DocumentFreshness {
	return {
		class: 'reference',
		verified_at: '2026-09-12T12:00:00Z',
		age_days: 18,
		due_at: '2026-10-10T12:00:00Z',
		state: 'current',
		owner_name: 'Structured Plans',
		...overrides,
	};
}

describe('formatFreshness', () => {
	it.each([
		[freshness({ state: 'current', age_days: 94 }), 'verified 94 days ago', false],
		[freshness({ state: 'overdue', age_days: 94 }), 'verified 94 days ago', true],
		[freshness({ state: 'never_verified', verified_at: null, age_days: null }), 'not yet verified', false],
		[freshness({ state: 'as_of' }), 'as of Sep 12', false],
		[freshness({ state: 'draft_expiring', age_days: 1 }), 'verified 1 day ago', false],
		[freshness({ state: 'archived' }), 'archived', false],
	] as const)('formats %s', (value, phrase, overdue) => {
		const result = formatFreshness(value);
		expect(result.phrase).toBe(phrase);
		expect(result.overdue).toBe(overdue);
	});

	it('provides the local short date for accessible time text', () => {
		expect(formatFreshness(freshness({ age_days: 94 })).dateText).toBe('Sep 12');
	});
});

describe('shouldOfferVerification', () => {
	it('shows for overdue and never verified documents', () => {
		expect(shouldOfferVerification(freshness({ state: 'overdue' }))).toBe(true);
		expect(shouldOfferVerification(freshness({ state: 'never_verified', verified_at: null, age_days: null }))).toBe(true);
	});

	it('shows in the seven-day window only', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
		expect(shouldOfferVerification(freshness({ due_at: '2026-10-07T12:00:00Z' }))).toBe(true);
		expect(shouldOfferVerification(freshness({ due_at: '2026-10-08T12:00:00Z' }))).toBe(false);
		expect(shouldOfferVerification(freshness({ due_at: '2026-09-29T12:00:00Z' }))).toBe(false);
		vi.useRealTimers();
	});
});
