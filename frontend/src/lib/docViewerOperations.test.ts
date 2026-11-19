import { describe, expect, it, vi } from 'vitest';

const { markArtifactVerified } = vi.hoisted(() => ({
	markArtifactVerified: vi.fn().mockResolvedValue({}),
}));

vi.mock('$lib/api/docs', async (importOriginal) => ({
	RequestRefused: (await importOriginal<typeof import('$lib/api/docs')>()).RequestRefused,
	markArtifactVerified,
	fetchContent: vi.fn(),
}));

vi.mock('$lib/stores/docComments', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/stores/docComments')>(),
	addReply: vi.fn(),
}));

import { addReply } from '$lib/stores/docComments';
import { shouldOfferVerification, type DocumentFreshness } from '$lib/docFreshness';
import { emit } from '$lib/ws/events';
import { createCommentRetryToast, createThreadActions, isNotCommentable, markFreshnessVerified, notCommentableMessage, postDocumentComment, subscribeToDocumentCommentChanges, submitDocumentComment } from './docViewerOperations';
import { toasts } from '$lib/stores/toast';
import { RequestRefused, type ServerComment } from '$lib/api/docs';
import { EarlierVersionPosted, EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import { keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';

function freshness(overrides: Partial<DocumentFreshness> = {}): DocumentFreshness {
	return {
		class: 'reference',
		verified_at: '2026-09-12T12:00:00Z',
		age_days: 91,
		due_at: '2026-10-03T12:00:00Z',
		state: 'overdue',
		owner_name: 'Structured Plans',
		...overrides,
	};
}

const NOT_COMMENTABLE_SENTENCE = 'Comments can’t be added to this document. Your comment wasn’t sent, and your text is still here.';

describe('comment submission failure delivery', () => {
	it('reports the failure and preserves its rejection for the draft-owning composer', async () => {
		const error = new Error('save refused');
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const rememberFailureToast = vi.fn();
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; }, rememberFailureToast)).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error');
			// The composer gets the toast to dismiss on a later success, and the reason to show beside its text.
			expect(rememberFailureToast).toHaveBeenCalledWith('toast', "Couldn't post the comment — try again");
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('shows the canonical failure toast when a caller has no viewer callback', async () => {
		const error = new Error('reply refused');
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'reply-toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; })).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error');
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it.each([
		['no_viewing_thread', 'Open this document from a chat to tag agents. Your comment is still here.'],
		['tag_unavailable', 'An agent you tagged can’t be tagged from this chat. Remove its tag and post again. Your comment is still here.'],
	])('names a %s tag refusal by its kind, ahead of the surface message', async (kind, copy) => {
		const error = new RequestRefused('400 {}', kind);
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; }, undefined, () => 'surface')).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith(copy, 'error');
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('shows the server\'s sentence for a not_commentable refusal as given, with no "try again", and keeps the rejection', async () => {
		const error = new RequestRefused(`409 ${JSON.stringify({ detail: NOT_COMMENTABLE_SENTENCE, kind: 'not_commentable' })}`, 'not_commentable');
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			await expect(submitDocumentComment(async () => { throw error; }, undefined, () => 'The document changed. Try again.')).rejects.toBe(error);
			expect(report).toHaveBeenCalledWith(NOT_COMMENTABLE_SENTENCE, 'error');
			expect(report.mock.calls[0][0]).not.toMatch(/try again/i);
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('matches the refusal by its kind, never by its sentence or the 409 status alone', () => {
		const body = JSON.stringify({ detail: NOT_COMMENTABLE_SENTENCE });
		expect(isNotCommentable(new RequestRefused(`409 ${body}`, 'not_commentable'))).toBe(true);
		expect(notCommentableMessage(new RequestRefused(`409 ${body}`, 'not_commentable'))).toBe(NOT_COMMENTABLE_SENTENCE);
		// Same sentence and status without the kind, another kind, a plain Error and a body with no sentence: none is this refusal.
		expect(notCommentableMessage(new RequestRefused(`409 ${body}`, null))).toBeUndefined();
		expect(notCommentableMessage(new RequestRefused(`409 ${body}`, 'diagram_changed'))).toBeUndefined();
		expect(notCommentableMessage(new Error(`409 ${body}`))).toBeUndefined();
		expect(notCommentableMessage(new RequestRefused('409 not json', 'not_commentable'))).toBeUndefined();
	});

	it('keeps a reply refused as not_commentable: the sentence shows, the rejection reaches the composer, and no other 409 is changed', async () => {
		const refused = new RequestRefused(`409 ${JSON.stringify({ detail: NOT_COMMENTABLE_SENTENCE, kind: 'not_commentable' })}`, 'not_commentable');
		const commentCreated = vi.fn();
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			vi.mocked(addReply).mockRejectedValueOnce(refused);
			await expect(createThreadActions(() => 'artifact', commentCreated).reply('root', 'My reply')).rejects.toBe(refused);
			expect(report).toHaveBeenCalledWith(NOT_COMMENTABLE_SENTENCE, 'error');
			expect(commentCreated).not.toHaveBeenCalled();
			// Another 409 has no named reason here, so the existing generic toast shows as on dev.
			const other = new RequestRefused('409 {"detail":"changed"}', null);
			vi.mocked(addReply).mockRejectedValueOnce(other);
			await expect(createThreadActions(() => 'artifact', commentCreated).reply('root', 'My reply')).rejects.toBe(other);
			expect(report).toHaveBeenLastCalledWith("Couldn't post the comment — try again", 'error');
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('rejects a missing artifact before attempting to post a comment', async () => {
		const ensureArtifactId = vi.fn().mockResolvedValue(null);
		const addComment = vi.fn();

		await expect(postDocumentComment(ensureArtifactId, addComment)).rejects.toThrow();

		expect(addComment).not.toHaveBeenCalled();
	});

	it('dismisses only the viewer instance’s remembered failure after a successful retry', () => {
		const dismiss = vi.fn();
		const firstViewer = createCommentRetryToast({ dismiss });
		const secondViewer = createCommentRetryToast({ dismiss });
		firstViewer.remember('first-failure');
		secondViewer.remember('second-failure');

		firstViewer.dismissAfterSuccess();

		expect(dismiss).toHaveBeenCalledTimes(1);
		expect(dismiss).toHaveBeenCalledWith('first-failure');
	});
});

describe('markFreshnessVerified', () => {
	it('replaces the canonical freshness state and clears a previously due document', async () => {
		const verified = await markFreshnessVerified('artifact-1', freshness());

		expect(markArtifactVerified).toHaveBeenCalledWith('artifact-1');
		expect(verified).toMatchObject({
			state: 'current',
			age_days: 0,
			due_at: null,
		});
		expect(shouldOfferVerification(verified)).toBe(false);
	});
});

describe('subscribeToDocumentCommentChanges', () => {
	it('reloads only the currently mounted artifact and unsubscribes on teardown', () => {
		const reload = vi.fn();
		const stop = subscribeToDocumentCommentChanges(() => 'artifact-current', reload);
		emit('doc_comment_changed', { artifact_id: 'artifact-other' });
		emit('doc_comment_changed', { artifact_id: 'artifact-current' });
		stop();
		emit('doc_comment_changed', { artifact_id: 'artifact-current' });
		expect(reload).toHaveBeenCalledTimes(1);
		expect(reload).toHaveBeenCalledWith('artifact-current');
	});
});

describe('a reply kept until it posts', () => {
	const key = replyDraftKey('scope', 'root');
	const row = { id: 'stored', author: 'you', author_kind: 'user', body: 'My reply', created_at: '2026-10-02T00:00:00Z' } as ServerComment;
	function quiet() {
		vi.mocked(addReply).mockReset();
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		return () => { report.mockRestore(); log.mockRestore(); keptCommentDrafts.reset(); vi.mocked(addReply).mockReset(); };
	}

	it('posts every press under the draft’s one id, keeps the reason a press failed, and lets it go once stored', async () => {
		const restore = quiet();
		try {
			const commentCreated = vi.fn();
			const actions = createThreadActions(() => 'artifact', commentCreated);
			keptCommentDrafts.keepReply(key, 'My reply', createMentionSelection());
			vi.mocked(addReply).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined);
			await expect(actions.reply('root', 'My reply', [], [], key)).rejects.toThrow('offline');
			const first = vi.mocked(addReply).mock.calls[0];
			expect(first[8]).toBe(false);
			expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'My reply', error: "Couldn't post the comment — try again", clientId: first[7] });
			await actions.reply('root', 'My reply', [], [], key);
			const retry = vi.mocked(addReply).mock.calls[1];
			expect(retry[7]).toBe(first[7]);
			expect(retry[8]).toBe(true);
			expect(keptCommentDrafts.get(key)).toBeUndefined();
			expect(commentCreated).toHaveBeenCalledOnce();
		} finally { restore(); }
	});

	it('keeps text typed while the reply posted, under a fresh id', async () => {
		const restore = quiet();
		try {
			const actions = createThreadActions(() => 'artifact', vi.fn());
			const tags = createMentionSelection();
			keptCommentDrafts.keepReply(key, 'My reply', tags);
			let finish!: () => void;
			vi.mocked(addReply).mockReturnValueOnce(new Promise<void>((resolve) => { finish = resolve; }));
			const posting = actions.reply('root', 'My reply', [], [], key);
			keptCommentDrafts.keepReply(key, 'My reply and more', tags);
			finish(); await posting;
			expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'My reply and more' });
			// Its own copy of the tags: the field that pressed clears its own once the reply is stored.
			expect(keptCommentDrafts.get(key)?.mentionSelection).not.toBe(tags);
			expect(keptCommentDrafts.get(key)?.clientId).toBeUndefined();
		} finally { restore(); }
	});

	it('keeps changes made after an unanswered press, with the sentence, under a fresh id, when the earlier version was posted', async () => {
		const restore = quiet();
		try {
			const actions = createThreadActions(() => 'artifact', vi.fn());
			const tags = createMentionSelection();
			keptCommentDrafts.keepReply(key, 'My reply', tags);
			keptCommentDrafts.claim(key, 'My reply', tags);
			keptCommentDrafts.keepReply(key, 'My reply, changed', tags);
			vi.mocked(addReply).mockRejectedValueOnce(new EarlierVersionPosted(row));
			await expect(actions.reply('root', 'My reply, changed', [], [], key)).rejects.toBeInstanceOf(EarlierVersionPosted);
			expect(vi.mocked(addReply).mock.calls[0][8]).toBe(true);
			expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'My reply, changed', error: EARLIER_VERSION_POSTED });
			expect(keptCommentDrafts.get(key)?.clientId).toBeUndefined();
		} finally { restore(); }
	});
});
