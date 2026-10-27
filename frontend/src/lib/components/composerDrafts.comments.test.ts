import { describe, expect, it } from 'vitest';
import { createComposerDrafts, type ComposerDraft } from './composerDrafts';

const comment = { id: 'c1', messageId: 'm1', seq: 1, from_off: 0, to_off: 3, quote: 'one', anchor_prefix: '', anchor_suffix: '', note: 'note' };

describe('comment drafts', () => {
	it('keeps comments with their thread when switching', () => {
		let thread = 'a';
		let current: ComposerDraft | null = { content: '', attachments: [], comments: [comment] };
		const drafts = createComposerDrafts({ current: () => current, restore: draft => { current = draft ?? null; }, reset: () => { current = null; }, boundThreadId: () => thread, hasText: () => false });
		drafts.switchThread('a');
		current = { content: '', attachments: [], comments: [comment] };
		thread = 'b'; drafts.switchThread('b');
		expect(current).toBeNull();
		thread = 'a'; drafts.switchThread('a');
		expect(current?.comments).toEqual([comment]);
	});
});
