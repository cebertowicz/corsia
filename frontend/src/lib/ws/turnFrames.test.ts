import { describe, expect, it } from 'vitest';
import { stepTextMessage } from './turnFrames';

describe('stepTextMessage', () => {
	it('uses the server projection, including an empty visible answer', () => {
		const frame = { message_id: 'm', content: '[c1.1] answer', created_at: 't', metadata: {}, chat_answer_cids: ['c1.1'], chat_visible_content: '' };
		expect(stepTextMessage(frame, 'thread', '')).toMatchObject({ chat_answer_cids: ['c1.1'], chat_visible_content: '' });
	});

	it('retains a partial tag prefix when the server did not place an answer', () => {
		const frame = { message_id: 'm', content: 'Intro\n\n[c1', created_at: 't', metadata: {} };
		const row = stepTextMessage(frame, 'thread', '');
		expect(row.content).toBe('Intro\n\n[c1');
		expect(row).not.toHaveProperty('chat_visible_content');
	});
});
