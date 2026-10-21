import { describe, expect, it } from 'vitest';
import { reviewStatus } from './reviewCopy';

describe('reviewStatus', () => {
	it('names an ended review the same whatever state it ended in', () => {
		for (const state of ['submitted', 'handed_back', 'failed'] as const) {
			expect(reviewStatus('ended_without_comments', state, 'Reviewer', '2026-10-21T09:00:00Z', 'limit')).toBe('Ended without comments');
		}
	});
	it('shows the request time for a running fork and the hand-back copy for an open continuation', () => {
		expect(reviewStatus('open', 'forked', 'Reviewer', '2026-10-21T09:00:00Z')).toMatch(/^Requested \d.*[AP]M$/);
		expect(reviewStatus('open', 'handed_back', 'Reviewer', '2026-10-21T09:00:00Z')).toBe('Unfinished. Reviewer will finish it.');
		expect(reviewStatus('open', 'failed', 'Reviewer', '2026-10-21T09:00:00Z', 'target_unavailable')).toBe('Couldn’t finish. Choose another model.');
	});
});
