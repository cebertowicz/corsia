import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { handleMessage } from '../ws/handlers';
import { messages, activeThreadId } from '../stores/threads';
import {
	_resetTurnStateForTests,
} from '../ws/turnState';

const THREAD_ID = 'test-session-chip';

const CONTEXT_EVENT_MSG = {
	id: 'chip-01',
	role: 'system',
	content: 'o/r#5 opened: Some fix',
	metadata: {
		type: 'context_event',
		event_type: 'webhook.github.pr_opened',
		summary: 'o/r#5 opened: Some fix',
		url: 'https://github.com/o/r/pull/5',
	},
	created_at: '2026-06-12T00:00:00',
};

function noop() {}

beforeEach(() => {
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
});

describe('context event message flow (L2 WS → store)', () => {
	it('message_added with a context event appends the exact row to the message store', () => {
		activeThreadId.set(THREAD_ID);

		handleMessage(
			{
				type: 'message_added',
				thread_id: THREAD_ID,
				message: CONTEXT_EVENT_MSG,
			},
			noop,
		);

		const stored = get(messages);
		const chip = stored.find((m) => m.id === 'chip-01');
		expect(chip).toBeDefined();
		expect(chip?.role).toBe('system');
		expect(chip?.metadata?.type).toBe('context_event');
		expect(chip?.metadata?.event_type).toBe('webhook.github.pr_opened');
		expect(chip?.metadata?.url).toBe('https://github.com/o/r/pull/5');
	});

	it('message_added for a different session does NOT appear in store', () => {
		activeThreadId.set('other-session');

		handleMessage(
			{
				type: 'message_added',
				thread_id: THREAD_ID,
				message: CONTEXT_EVENT_MSG,
			},
			noop,
		);

		const stored = get(messages);
		expect(stored.find((m) => m.id === 'chip-01')).toBeUndefined();
	});

});
