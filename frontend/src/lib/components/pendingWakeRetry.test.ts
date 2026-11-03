import { describe, expect, it } from 'vitest';
import { needsWakeRetry } from './pendingWakeRetry';
import type { TranscriptMessage } from './messageListModel';

function rerun(metadata: Record<string, unknown>): TranscriptMessage {
	return {
		id: 'overflow-continue:t1', role: 'system', content: 'Continue the work that turn was doing.',
		delivery_state: 'failed',
		metadata: { type: 'context_event', event_type: 'context_overflow.continue', event_disposition: 'wake', ...metadata },
	} as unknown as TranscriptMessage;
}

describe('a re-run that could not be delivered', () => {
	it('offers no Retry row, so its own text never shows: the refused Turn’s notice stands alone', () => {
		expect(needsWakeRetry(rerun({}))).toBe(false);
		expect(needsWakeRetry(rerun({ summary: 'Make me a plan for the migration.' }))).toBe(false);
	});
});
