import { describe, expect, it } from 'vitest';
import { reviewStatus } from './reviewCopy';

describe('reviewStatus', () => {
	it('distinguishes empty, singular, and plural completed reviews', () => {
		expect(reviewStatus('submitted', 'Reviewer', '2026-10-21T09:00:00Z', null, 0)).toBe('Finished without comments.');
		expect(reviewStatus('submitted', 'Reviewer', '2026-10-21T09:00:00Z', null, 1)).toBe('Finished with 1 comment.');
		expect(reviewStatus('submitted', 'Reviewer', '2026-10-21T09:00:00Z', null, 2)).toBe('Finished with 2 comments.');
	});
});
