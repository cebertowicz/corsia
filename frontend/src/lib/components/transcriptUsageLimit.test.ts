import { describe, expect, it } from 'vitest';
import { isFocusVisible, isNeverDrawn, isSystemNotice, pairUnansweredInput } from './transcriptRowKinds';
import type { TranscriptMessage } from './messageListModel';

function row(metadata: Record<string, unknown>, content = 'Error'): TranscriptMessage {
	return { id: 'row', thread_id: 'thread', role: 'system', content, metadata } as TranscriptMessage;
}

describe('usage limit transcript visibility', () => {
	it.each([
		{ cc: true, type: 'result', is_error: true, turn_id: 'turn', stop_reason: 'stop_sequence', cause_kind: 'usage_limit', resets_at: '2026-11-14T10:40:00-08:00', model: 'opus', next_step: 'answered' },
		{ codex: true, type: 'result', is_error: true, turn_id: 'turn', chat_drawn: false },
		{ type: 'cc_error', cause_kind: 'usage_limit' },
		{ type: 'llm_error', cause_kind: 'usage_limit' },
		{ type: 'context_event', event_type: 'vendor_usage_limit_refusal' },
	])('hides %o', (metadata) => {
		const message = row(metadata);
		expect(isNeverDrawn(message)).toBe(true);
		expect(isSystemNotice(message)).toBe(false);
		expect(isFocusVisible(message)).toBe(false);
	});

	it.each(['sign_in_expired', 'context_overflow', 'rate_limited', 'process_exited'])('retains %s notices', (kind) => {
		const message = row({ type: 'cc_error', cause_kind: kind });
		expect(isNeverDrawn(message)).toBe(false);
		expect(isSystemNotice(message)).toBe(true);
	});

	it('removes a retry paired with a hidden limit notice', () => {
		const notice = row({ type: 'result', is_error: true, turn_id: 't', cause_kind: 'usage_limit' });
		const retry = row({ event_type: 'system.turn.unanswered_input', failed_turn_id: 't' });
		expect(pairUnansweredInput([notice, retry]).filter((message) => !isNeverDrawn(message))).toEqual([]);
	});
});
