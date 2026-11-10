import { describe, expect, it } from 'vitest';
import { addedMessage } from './messageProjection';

const payload = { id: 'update', role: 'assistant', content: '[c1.1] answer', metadata: { for_user: true }, created_at: 't' };

describe('an added message', () => {
	it('keeps the placed answers the server projected for it', () => {
		const message = addedMessage({ ...payload, chat_answer_cids: ['c1.1'], chat_visible_content: '' }, 'thread');
		expect(message).toMatchObject({ chat_answer_cids: ['c1.1'], chat_visible_content: '' });
	});

	it('carries no placed answers when the server named none', () => {
		const message = addedMessage(payload, 'thread');
		expect(message).not.toHaveProperty('chat_answer_cids');
		expect(message).not.toHaveProperty('chat_visible_content');
	});
});
