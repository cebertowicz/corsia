import { describe, expect, it } from 'vitest';
import { needsWakeRetry, wakeSummary } from './pendingWakeRetry';
import type { TranscriptMessage } from './messageListModel';

function rerun(metadata: Record<string, unknown>): TranscriptMessage {
	return {
		id: 'overflow-continue:t1', role: 'system', content: 'Continue the work that turn was doing.',
		delivery_state: 'failed',
		metadata: { type: 'context_event', event_type: 'context_overflow.continue', event_disposition: 'wake', ...metadata },
	} as unknown as TranscriptMessage;
}

describe('a re-run that could not be delivered', () => {
	it('is named by the person message it runs again', () => {
		const msg = rerun({ summary: 'Make me a plan for the migration.' });
		expect(needsWakeRetry(msg)).toBe(true);
		expect(wakeSummary(msg)).toBe('Make me a plan for the migration.');
	});

	it('offers no Retry row when no person message stands behind it, so its own text never shows', () => {
		expect(needsWakeRetry(rerun({}))).toBe(false);
	});
});
