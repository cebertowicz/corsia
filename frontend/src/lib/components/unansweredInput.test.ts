import { describe, expect, it } from 'vitest';
import { buildVisibleMessages } from './messageListModel';

const error = { id: 'error', thread_id: 't', role: 'system', content: 'Provider failed.', metadata: { type: 'llm_error', turn_id: 'one' } };
const wake = { id: 'wake', thread_id: 't', role: 'system', content: 'The agent tried once more.', metadata: { type: 'activity_receipt', event_type: 'system.turn.unanswered_input', failed_turn_id: 'one' } };

describe('unanswered input retry', () => {
	it('appends to the matching notice and removes the event row', () => {
		const result = buildVisibleMessages([error, wake]);
		expect(result).toHaveLength(1);
		expect(result[0].content).toBe('Provider failed. The agent tried once more.');
		expect(result[0].id).toBe('error');
	});

	it('ends an unpunctuated notice as a sentence before the retry sentence', () => {
		const result = buildVisibleMessages([{ ...error, content: 'Error: Internal server error ' }, wake]);
		expect(result[0].content).toBe('Error: Internal server error. The agent tried once more.');
	});

	it('leaves an unmatched retry visible and does not pair across threads', () => {
		const result = buildVisibleMessages([error, { ...wake, thread_id: 'other' }]);
		expect(result.map((row) => row.id)).toEqual(['error', 'wake']);
		expect(result[0].content).toBe('Provider failed.');
	});
});
